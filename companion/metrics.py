"""Deterministic measurements from native game telemetry, without model inference."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from statistics import median

SCHEMA_VERSION = 1
METRIC_VERSION = 2
MAX_RECORD_BYTES = 24 * 1024 * 1024
MAX_SAMPLES = 120_000
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
DRILLS = {"clicking", "tracking", "switching"}
TRAINING_KINDS = {"baseline", "practice", "retest", "free", "sensitivity"}
SHOT_POSITION_FIELDS = {"aim_yaw", "aim_pitch", "target_yaw", "target_pitch", "target_radius"}


def canonicalize_json(value: object) -> object:
    """Copy JSON data with integral floats normalized for Godot round-trip identity."""
    def normalize(item):
        if item is None or type(item) in (bool, int, str):
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("Record must be finite JSON data")
            return int(item) if item.is_integer() else item
        if isinstance(item, list):
            return [normalize(child) for child in item]
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON object keys must be strings")
            return {key: normalize(child) for key, child in item.items()}
        raise ValueError("Record must contain only JSON data")

    try:
        return normalize(value)
    except RecursionError as exc:
        raise ValueError("Record JSON is nested too deeply") from exc


def validate_id(value: object) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValueError("Record id must contain 1–96 letters, numbers, underscores or hyphens")
    return value


def _number(value: object, field: str, minimum: float | None = None,
            maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number")
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{field} is out of range")
    return float(value)


def _target_id(value: object) -> None:
    if not isinstance(value, str) or len(value) > 96:
        raise ValueError("target_id must be a string of at most 96 characters")


def validate_training_context(context: object) -> dict:
    """Validate optional training links without changing telemetry or benchmark identity."""
    if not isinstance(context, dict):
        raise ValueError("training_context must be an object")
    if not context:
        return context
    allowed = {"kind", "cycle_id", "baseline_record_id", "source_coaching_record_id", "block_index"}
    if set(context) - allowed:
        raise ValueError("training_context contains unsupported properties")
    if not isinstance(context.get("kind"), str) or context["kind"] not in TRAINING_KINDS:
        raise ValueError("Unknown training_context.kind")
    for name in ("cycle_id", "baseline_record_id", "source_coaching_record_id"):
        if name in context and context[name] != "":
            validate_id(context[name])
    if "block_index" in context:
        index = _number(context["block_index"], "training_context.block_index", 0, 10_000)
        if not index.is_integer():
            raise ValueError("training_context.block_index must be an integer")
    return context


def validate_record(record: dict) -> dict:
    """Reject malformed/unbounded records before calculating or writing anything."""
    if not isinstance(record, dict):
        raise ValueError("Record must be an object")
    if type(record.get("schema_version")) not in (int, float) or record.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported record schema_version")
    validate_id(record.get("id"))
    if not isinstance(record.get("drill"), str) or record.get("drill") not in DRILLS:
        raise ValueError("Unknown drill")
    _number(record.get("started_at"), "started_at", 0, 1e12)
    duration = _number(record.get("duration_s"), "duration_s", 0, 3600)
    if not isinstance(record.get("completed"), bool):
        raise ValueError("completed must be boolean")
    if "training_context" in record:
        validate_training_context(record["training_context"])
    settings = record.get("settings")
    if not isinstance(settings, dict) or len(settings) > 64 or any(not isinstance(k, str) for k in settings):
        raise ValueError("settings must be an object with at most 64 named properties")
    for field, low, high in (("fov", 10, 170), ("sensitivity_deg_per_count", .00001, 10),
                             ("target_scale", .05, 20), ("speed_scale", .05, 20)):
        _number(settings.get(field), field, low, high)
    seed = settings.get("seed")
    if not (type(seed) in (str, int) or
            type(seed) is float and math.isfinite(seed) and seed.is_integer()):
        raise ValueError("settings.seed must be an integer or string")
    for name, maximum in (("samples", MAX_SAMPLES), ("shots", 20_000), ("events", 40_000)):
        items = record.get(name)
        if not isinstance(items, list) or len(items) > maximum:
            raise ValueError(f"{name} must be a list with at most {maximum} entries")
        last_t = -1.0
        for item in items:
            if not isinstance(item, dict):
                raise ValueError(f"{name} entries must be objects")
            t = _number(item.get("t"), f"{name}.t", 0, duration + .1)
            if t < last_t:
                raise ValueError(f"{name} timestamps must be ordered")
            last_t = t
            _target_id(item.get("target_id"))
            if name == "samples":
                for field in ("yaw", "target_yaw"):
                    _number(item.get(field), field, -1e8, 1e8)
                for field in ("pitch", "target_pitch"):
                    _number(item.get(field), field, -90, 90)
                _number(item.get("target_radius"), "target_radius", .001, 90)
                _number(item.get("frame_ms"), "frame_ms", 0, 60_000)
                for field in ("on_target", "engaged"):
                    if not isinstance(item.get(field), bool):
                        raise ValueError(f"{field} must be boolean")
            elif name == "shots":
                if not isinstance(item.get("hit"), bool):
                    raise ValueError("shot.hit must be boolean")
                _number(item.get("error_deg"), "error_deg", 0, 180)
                if SHOT_POSITION_FIELDS & item.keys():
                    if not SHOT_POSITION_FIELDS <= item.keys():
                        raise ValueError("Shot position requires all aim and target angles and radius")
                    for field in ("aim_yaw", "target_yaw"):
                        _number(item[field], "shot." + field, -1e8, 1e8)
                    for field in ("aim_pitch", "target_pitch"):
                        _number(item[field], "shot." + field, -90, 90)
                    _number(item["target_radius"], "shot.target_radius", .001, 90)
                    measured_error = angular_error(item["aim_yaw"], item["aim_pitch"], item["target_yaw"], item["target_pitch"])
                    if abs(measured_error - item["error_deg"]) > .005:
                        raise ValueError("Shot position does not match its recorded angular error")
                    if item["hit"] and measured_error > item["target_radius"] + .005:
                        raise ValueError("Successful shot position is outside the target")
            else:
                if item.get("type") not in {"spawn", "hit", "despawn"}:
                    raise ValueError("Unknown event type")
                _number(item.get("yaw"), "event.yaw", -1e8, 1e8)
                _number(item.get("pitch"), "event.pitch", -90, 90)
                _number(item.get("radius"), "event.radius", .001, 90)
    try:
        encoded = json.dumps(record, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("Record must be finite JSON data") from exc
    if len(encoded.encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValueError("Record exceeds the 24 MiB limit")
    return record


def angle_delta(a: float, b: float) -> float:
    """Signed shortest difference a-b in degrees, including yaw wraparound."""
    return (a - b + 180.0) % 360.0 - 180.0


def angular_error(yaw: float, pitch: float, target_yaw: float, target_pitch: float) -> float:
    """Great-circle angular separation of two viewing directions, in degrees."""
    p1, p2 = math.radians(pitch), math.radians(target_pitch)
    d_yaw = math.radians(angle_delta(yaw, target_yaw))
    cosine = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(d_yaw)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def shot_position(shot: dict) -> dict | None:
    """Exact shot-time angular offset; x is target-right, y is target-up, radius 1 is edge.

    This locates the aim ray within the target's angular silhouette, not a surface UV
    or a reconstructed bullet decal. Older shots have no direction and stay unknown.
    """
    if not SHOT_POSITION_FIELDS <= shot.keys():
        return None
    yaw, pitch = math.radians(shot["aim_yaw"]), math.radians(shot["aim_pitch"])
    target_yaw, target_pitch = math.radians(shot["target_yaw"]), math.radians(shot["target_pitch"])
    aim = (math.sin(yaw) * math.cos(pitch), math.sin(pitch), -math.cos(yaw) * math.cos(pitch))
    right = (math.cos(target_yaw), 0.0, math.sin(target_yaw))
    up = (-math.sin(target_yaw) * math.sin(target_pitch), math.cos(target_pitch),
          math.cos(target_yaw) * math.sin(target_pitch))
    x = sum(a * b for a, b in zip(aim, right))
    y = sum(a * b for a, b in zip(aim, up))
    length = math.hypot(x, y)
    error = angular_error(shot["aim_yaw"], shot["aim_pitch"], shot["target_yaw"], shot["target_pitch"])
    radial = error / shot["target_radius"]
    if length < 1e-12:
        # At the exact antipode there is no unique direction around the target.
        x, y = (0.0, 0.0) if error < 1e-5 else (None, None)
    else:
        x, y = x / length * radial, y / length * radial
    return {"x_radius": x, "y_radius": y, "radial_radius": radial}


def _shot_placement(shots: list[dict]) -> tuple[dict, list[dict]]:
    positioned = [(shot, shot_position(shot)) for shot in shots]
    located = [(shot, offset) for shot, offset in positioned if offset is not None]
    hit_offsets = [offset for shot, offset in located if shot["hit"]]
    total_hits = sum(shot["hit"] for shot in shots)
    center = sum(offset["radial_radius"] <= .5 + 1e-7 for offset in hit_offsets)
    edge = sum(offset["radial_radius"] >= .8 - 1e-7 for offset in hit_offsets)
    directions = {name: sum(offset[axis] is not None and sign * offset[axis] > 1e-6 for offset in hit_offsets)
                  for name, axis, sign in (("left", "x_radius", -1), ("right", "x_radius", 1),
                                           ("below", "y_radius", -1), ("above", "y_radius", 1))}
    summary = {"available": bool(hit_offsets), "recorded_shots": len(shots),
        "located_shots": len(located), "recorded_hits": total_hits, "located_hits": len(hit_offsets),
        "unknown_position_hits": total_hits - len(hit_offsets),
        "coordinates": "Exact shot-time angular offset from target center: right/up positive; 100% of target angular radius is the edge. Not a 3D surface impact.",
        "zones": {"center": center, "middle": len(hit_offsets) - center - edge, "edge": edge},
        "zone_definition": "Center: inner half-radius. Edge: outer 20% of radius. Zones describe placement, not extra score.",
        "hit_directions": directions,
        "direction_definition": "Horizontal and vertical counts overlap; a top-right hit counts as both above and right."}
    examples = [{"t": shot["t"], "target_id": shot["target_id"], "hit": shot["hit"],
                 **{name: _rounded(value) for name, value in offset.items()}}
                for shot, offset in located[:6]]
    return summary | {"examples": examples}, hit_offsets


def benchmark_key(record: dict) -> str:
    # Any future setting conservatively creates a new benchmark, except the seed.
    settings = {k: v for k, v in record["settings"].items() if k != "seed"}
    payload = {"schema_version": record["schema_version"], "metric_version": METRIC_VERSION,
               "drill_version": record.get("drill_version", 1), "drill": record["drill"],
               "settings": settings}
    serialized = json.dumps(canonicalize_json(payload), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(serialized.encode()).hexdigest()[:24]


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    return ordered[lower] * (upper - index) + ordered[upper] * (index - lower) if lower != upper else ordered[lower]


def _rounded(value: float | int | None) -> float | int | None:
    return round(value, 4) if isinstance(value, float) else value


def _trial_measurements(record: dict) -> tuple[list[dict], list[dict]]:
    """Completed spawn/hit trials; static traces support conservative overshoot."""
    samples_by_target = defaultdict(list)
    for sample in record["samples"]:
        if sample["engaged"]:
            samples_by_target[sample["target_id"]].append(sample)
    hits_by_target = defaultdict(list)
    for shot in record["shots"]:
        if shot["hit"]:
            hits_by_target[shot["target_id"]].append(shot)
    spawns_by_target = defaultdict(list)
    for event in record["events"]:
        if event["type"] == "spawn":
            spawns_by_target[event["target_id"]].append(event)
    trials, eligible = [], []
    for target_id, spawns in spawns_by_target.items():
        for index, spawn in enumerate(spawns):
            until = spawns[index + 1]["t"] if index + 1 < len(spawns) else math.inf
            hit = next((h for h in hits_by_target[target_id] if spawn["t"] <= h["t"] < until), None)
            if hit is None:
                continue
            trial = {"target_id": target_id, "spawn_t": spawn["t"], "hit_t": hit["t"],
                     "acquisition_ms": (hit["t"] - spawn["t"]) * 1000}
            trials.append(trial)
            if record["drill"] == "tracking":
                continue
            trace = [s for s in samples_by_target[target_id] if spawn["t"] <= s["t"] <= hit["t"]]
            if (len(trace) < 6 or trace[0]["t"] > spawn["t"] + .1 or
                    hit["t"] - trace[-1]["t"] > .1 or
                    any(b["t"] - a["t"] > .08 for a, b in zip(trace, trace[1:]))):
                continue
            first = trace[0]
            radius = first["target_radius"]
            if any(angular_error(s["target_yaw"], s["target_pitch"], first["target_yaw"],
                                 first["target_pitch"]) > min(.2, radius * .2) for s in trace):
                continue
            # Tangent-plane projection is only reliable away from the poles, and
            # for modest flick angles; other trials deliberately remain unscored.
            if abs(first["target_pitch"]) > 60:
                continue
            cos_pitch = math.cos(math.radians(first["target_pitch"]))
            initial_x = angle_delta(first["yaw"], first["target_yaw"]) * cos_pitch
            initial_y = first["pitch"] - first["target_pitch"]
            distance = math.hypot(initial_x, initial_y)
            if distance <= radius * 1.5 or distance > 45:
                continue
            projections = [(s, (angle_delta(s["yaw"], first["target_yaw"]) * cos_pitch * initial_x +
                                 (s["pitch"] - first["target_pitch"]) * initial_y) / distance) for s in trace]
            overshot = next((s for s, projection in projections if projection < -radius), None)
            item = dict(trial, overshot=overshot is not None,
                        correction_ms=(hit["t"] - overshot["t"]) * 1000 if overshot else None)
            # Evidence retains the first, last and the farthest-past-target sample.
            selected = {0, len(trace) - 1, min(range(len(projections)), key=lambda i: projections[i][1])}
            selected.update(range(0, len(trace), max(1, len(trace) // 10)))
            item["trace"] = [{k: _rounded(s[k]) for k in ("t", "yaw", "pitch", "target_yaw", "target_pitch", "target_radius")}
                             for i, s in enumerate(trace) if i in selected][:14]
            eligible.append(item)
    return trials, eligible


def analyze_record(record: dict) -> dict:
    validate_record(record)
    samples, shots = record["samples"], record["shots"]
    metrics: dict = {}
    evidence: list[dict] = []

    def add(name: str, value: float | int | None, unit: str, description: str, ids: list[str]) -> None:
        metrics[name] = {"value": _rounded(value), "unit": unit, "description": description,
                         "evidence_ids": ids if value is not None else []}

    hits = sum(shot["hit"] for shot in shots)
    evidence.append({"id": "shots-summary", "kind": "aggregate", "summary": "Recorded shot outcomes",
                     "data": {"shots": len(shots), "hits": hits}})
    add("shots", len(shots), "count", "Number of recorded shots, including misses.", ["shots-summary"])
    add("hits", hits, "count", "Number of recorded successful shots.", ["shots-summary"])
    add("accuracy_pct", hits / len(shots) * 100 if shots else None, "%",
        "Successful shots divided by all recorded shots; not tracking time on target.", ["shots-summary"])

    placement, hit_offsets = _shot_placement(shots)
    located_hits = len(hit_offsets)
    evidence.append({"id": "shot-placement-summary", "kind": "aggregate",
        "summary": "Exact click locations relative to target center; older unlocated shots are excluded",
        "data": placement})
    placement_evidence = ["shot-placement-summary"]
    add("hit_position_coverage_pct", located_hits / hits * 100 if hits else None, "%",
        "Share of successful shots with exact shot-time position data; older hits without it are unknown.", placement_evidence)
    add("mean_hit_offset_pct_radius", sum(item["radial_radius"] for item in hit_offsets) / located_hits * 100 if located_hits else None,
        "% radius", "Mean angular distance from target center among located hits: 0=center, 100=edge.", placement_evidence)
    add("center_hit_pct", placement["zones"]["center"] / located_hits * 100 if located_hits else None, "%",
        "Share of located successful shots within the inner half of the target angular radius.", placement_evidence)
    add("edge_hit_pct", placement["zones"]["edge"] / located_hits * 100 if located_hits else None, "%",
        "Share of located successful shots in the outer 20% of target angular radius; ordinary hits still score equally.", placement_evidence)
    for axis, name, unit in (("x_radius", "hit_horizontal_bias_pct_radius", "% radius; right+"),
                             ("y_radius", "hit_vertical_bias_pct_radius", "% radius; up+")):
        values = [item[axis] for item in hit_offsets if item[axis] is not None]
        add(name, sum(values) / len(values) * 100 if values else None, unit,
            "Mean signed target-relative offset among located hits; opposite sides can cancel, so read with mean hit distance.", placement_evidence)

    intervals = [b["t"] - a["t"] for a, b in zip(samples, samples[1:]) if b["t"] > a["t"]]
    span = samples[-1]["t"] - samples[0]["t"] if len(samples) > 1 else 0
    hz = (len(intervals) / span) if span else 0
    weighted_error, target_time, observed_time = 0.0, 0.0, 0.0
    trace_candidates = []
    for a, b in zip(samples, samples[1:]):
        dt = b["t"] - a["t"]
        if (not a["engaged"] or not b["engaged"] or a["target_id"] != b["target_id"] or
                not 0 < dt <= .1):
            continue
        error = angular_error(a["yaw"], a["pitch"], a["target_yaw"], a["target_pitch"])
        weighted_error += error * dt
        target_time += dt * a["on_target"]
        observed_time += dt
        trace_candidates.append((error, a))
    evidence.append({"id": "aim-summary", "kind": "aggregate", "summary": "Time-weighted sampled aim measurements",
                     "data": {"observed_s": round(observed_time, 4), "on_target_s": round(target_time, 4),
                              "samples": len(samples), "sample_hz": round(hz, 2),
                              "max_sample_gap_s": round(max(intervals, default=0), 4)}})
    add("tracking_error_deg", weighted_error / observed_time if observed_time else None, "deg",
        "Time-weighted angular distance from target center while engaged; gaps over 100 ms and target transitions excluded.", ["aim-summary"])
    add("time_on_target_pct", target_time / observed_time * 100 if observed_time else None, "%",
        "Percentage of observed engaged time reported on target; excludes gaps over 100 ms and target transitions.", ["aim-summary"])
    if trace_candidates:
        ordered = sorted(trace_candidates, key=lambda pair: pair[0])
        selected = [ordered[0], ordered[len(ordered)//2], ordered[-1]]
        evidence.append({"id": "aim-examples", "kind": "samples", "summary": "Best, median and worst sampled angular errors; not consecutive motion",
                         "data": [{"error_deg": round(error, 4), **{k: _rounded(s[k]) for k in
                                   ("t", "yaw", "pitch", "target_yaw", "target_pitch", "target_radius", "target_id")}}
                                  for error, s in selected]})
        metrics["tracking_error_deg"]["evidence_ids"].append("aim-examples")

    trials, eligible = _trial_measurements(record)
    evidence.append({"id": "acquisition-summary", "kind": "aggregate", "summary": "Completed target presentations",
                     "data": {"completed_targets": len(trials), "examples": trials[:5]}})
    add("acquisition_ms", median([t["acquisition_ms"] for t in trials]) if trials else None, "ms",
        "Median elapsed time from target spawn to its first successful shot; simultaneous target spawns include waiting time.", ["acquisition-summary"])
    successful = [s for s in shots if s["hit"]]
    hit_intervals = [(b["t"] - a["t"]) * 1000 for a, b in zip(successful, successful[1:])
                     if a["target_id"] != b["target_id"]]
    evidence.append({"id": "hit-interval-summary", "kind": "aggregate", "summary": "Intervals between consecutive hits on different targets",
                     "data": {"intervals": len(hit_intervals), "example_ms": [round(t, 3) for t in hit_intervals[:8]]}})
    add("hit_interval_ms", median(hit_intervals) if hit_intervals else None, "ms",
        "Median time between consecutive successful shots on different target ids, including any intervening misses.", ["hit-interval-summary"])
    overshoots = [t for t in eligible if t["overshot"]]
    evidence.append({"id": "overshoot-summary", "kind": "aggregate", "summary": "Conservatively measured static-target overshoot",
                     "data": {"eligible_trials": len(eligible), "overshot_trials": len(overshoots),
                              "examples": (overshoots[:2] + [t for t in eligible if not t["overshot"]][:2])}})
    add("overshoot_pct", len(overshoots) / len(eligible) * 100 if eligible else None, "%",
        "Share of eligible static-target trials crossing beyond the target's far edge along the initial approach direction before hitting; excludes unsupported traces.", ["overshoot-summary"])
    add("correction_ms", median([t["correction_ms"] for t in overshoots]) if overshoots else None, "ms",
        "Median elapsed time from first sampled far-edge crossing to successful shot, for measured overshoots only; this is not biological reaction time.", ["overshoot-summary"])
    frame_times = [s["frame_ms"] for s in samples if s["frame_ms"] > 0]
    frame_p95 = _percentile(frame_times, .95)
    evidence.append({"id": "frame-summary", "kind": "aggregate", "summary": "Frame times captured at telemetry samples",
                     "data": {"sampled_frames": len(frame_times), "p95_ms": _rounded(frame_p95)}})
    add("frame_p95_ms", frame_p95, "ms", "95th percentile of sampled frame times; spikes between telemetry samples may be missed.", ["frame-summary"])

    flags = []
    if not record["completed"]:
        flags.append("incomplete_run")
    if record["duration_s"] < 15:
        flags.append("short_run")
    if record["drill"] != "tracking" and len(shots) < 10:
        flags.append("low_attempts")
    if record["drill"] == "tracking" and observed_time + 1e-6 < 15:
        flags.append("low_tracking_engagement")
    if hz < 20:
        flags.append("low_sample_cadence")
    if intervals and sum(dt > .1 for dt in intervals) / len(intervals) > .02:
        flags.append("sample_gaps")
    if span < record["duration_s"] * .8:
        flags.append("low_sample_coverage")
    valid = bool(len(samples) >= 2 and (observed_time > 0 or shots))
    usable = valid and record["completed"] and not any(f in flags for f in
                ("short_run", "low_attempts", "low_tracking_engagement", "low_sample_cadence", "sample_gaps", "low_sample_coverage"))
    # A citation must identify its originating round, even in multi-session context.
    prefix = record["id"] + ":"
    for item in evidence:
        item["id"] = prefix + item["id"]
    for item in metrics.values():
        item["evidence_ids"] = [prefix + identifier for identifier in item["evidence_ids"]]
    report = {"schema_version": SCHEMA_VERSION, "metric_version": METRIC_VERSION,
            "record_id": record["id"], "drill": record["drill"], "started_at": record["started_at"],
            "duration_s": record["duration_s"], "benchmark_key": benchmark_key(record), "valid": valid,
            "quality": {"flags": flags, "usable_for_coaching": usable, "sample_hz": round(hz, 2)},
            "metrics": metrics, "evidence": evidence}
    report["shot_placement"] = {key: value for key, value in placement.items() if key != "examples"}
    report["shot_placement"]["evidence_id"] = prefix + "shot-placement-summary"
    if "training_context" in record:
        report["training_context"] = canonicalize_json(record["training_context"])
    if record["drill"] == "tracking":
        report["tracking_motion"] = record["settings"].get("tracking_motion", "smooth")
    return report
