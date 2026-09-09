"""One persisted comparison and coaching memory shared by the UI and the coach."""
from __future__ import annotations

import math
import copy

from .metrics import canonicalize_json


def _kind(report: dict) -> str:
    return report.get("training_context", {}).get("kind", "free")


def comparison_report(storage, report: dict) -> dict | None:
    if _kind(report) in {"practice", "sensitivity"}:
        return None
    baseline_id = report.get("training_context", {}).get("baseline_record_id")
    if baseline_id:
        baseline = storage.get_report(baseline_id)
        if (baseline.get("benchmark_key") == report.get("benchmark_key")
                and baseline.get("drill") == report.get("drill")
                and baseline.get("started_at", 0) < report.get("started_at", 0)):
            return baseline
        # An explicit baseline must never silently turn into a practice score.
        return None
    return next((item for item in storage.matching_history(report, limit=100)
                 if _kind(item) not in {"practice", "sensitivity"}), None)


def benchmark_comparison(storage, report: dict) -> dict | None:
    previous = comparison_report(storage, report)
    if previous is None:
        return None
    metrics = {}
    for name, current_metric in report.get("metrics", {}).items():
        previous_metric = previous.get("metrics", {}).get(name, {})
        current, before = current_metric.get("value"), previous_metric.get("value")
        if (all(type(value) in (int, float) and math.isfinite(value) for value in (current, before))
                and current_metric.get("unit") == previous_metric.get("unit")):
            metrics[name] = {"previous": before, "current": current, "delta": current - before,
                             "unit": current_metric.get("unit")}
    explicit = bool(report.get("training_context", {}).get("baseline_record_id"))
    return {"previous_record_id": previous["record_id"], "metrics": metrics,
            "kind": "baseline" if explicit else "previous_matching_round"}


def contextual_history(storage, report: dict) -> list[dict]:
    """Include useful practice evidence while identifying the actual comparison."""
    history = [item for item in storage.matching_history(report, limit=100)
               if _kind(item) != "sensitivity"][:5]
    selected = {report["record_id"], *(item["record_id"] for item in history)}
    training = report.get("training_context", {})
    comparison = comparison_report(storage, report)
    links = [training.get("baseline_record_id"), training.get("source_coaching_record_id")]
    if comparison:
        links.append(comparison["record_id"])
    for record_id in links:
        if record_id and record_id not in selected:
            linked = storage.get_report(record_id)
            if linked.get("started_at", 0) <= report.get("started_at", 0) and _kind(linked) != "sensitivity":
                history.append(linked)
                selected.add(record_id)
    seen_modes = {_mode(report), *(_mode(item) for item in history)}
    # Tagged baselines use their cycle, not nearby free practice. Legacy rounds
    # retain the earlier short-window behavior until they acquire explicit links.
    for summary in [*storage.list_sessions(limit=100), *_baseline_reports(storage)]:
        record_id = summary["record_id"]
        if record_id in selected or _mode(summary) in seen_modes or _kind(summary) in {"practice", "sensitivity"}:
            continue
        age = report.get("started_at", 0) - summary.get("started_at", 0)
        if age < 0 or (not training.get("cycle_id") and age > 15 * 60):
            continue
        if training.get("cycle_id") and summary.get("training_context", {}).get("cycle_id") != training["cycle_id"]:
            continue
        other = storage.get_report(record_id)
        history.append(other)
        selected.add(record_id)
        seen_modes.add(_mode(other))
        if len(seen_modes) == 4:
            break
    for item in history:
        same = item.get("benchmark_key") == report.get("benchmark_key")
        item["comparison_role"] = ("practice" if _kind(item) == "practice" else
                                   "same_benchmark" if same else "recent_other_drill")
        item["benchmark_matches"] = same
    return history


def coaching_context(storage, report: dict) -> dict:
    journal = storage.coaching_history(report, limit=6)
    # Sensitivity screening has its own bounded evidence and review contract.
    journal = [entry for entry in journal
               if _kind(storage.get_report(entry["record_id"])) != "sensitivity"]
    return {"training_context": report.get("training_context", {}),
            "comparison": benchmark_comparison(storage, report), "journal": journal}


BASELINE_MODES = ("clicking", "tracking", "reactive_tracking", "switching")


def _baseline_reports(storage) -> list[dict]:
    return storage.baseline_history(limit=100)


def _mode(item: dict) -> str:
    if item.get("drill") == "tracking":
        motion = item.get("settings", {}).get("tracking_motion", item.get("tracking_motion", "smooth"))
        return "reactive_tracking" if motion == "reactive" else "tracking"
    return item.get("drill", "")


def _clean_settings(settings: dict) -> dict:
    return canonicalize_json({key: value for key, value in settings.items() if key != "seed"})


def _profile_matches(record: dict, settings: dict | None) -> bool:
    # Round length/target size/speed are prescribed challenges. Changing the
    # player's sensitivity or field of view requires a fresh matching baseline.
    return all(record["settings"].get(key) == settings[key]
               for key in ("sensitivity_deg_per_count", "fov") if settings and key in settings)


def _snapshot(record: dict | None) -> dict | None:
    if record is None:
        return None
    return copy.deepcopy({key: record[key] for key in (
        "id", "drill", "settings", "started_at", "completed", "duration_s",
        "schema_version", "drill_version", "training_context") if key in record})


def _usable(report: dict) -> bool:
    return (report.get("valid") is True and report.get("quality", {}).get("usable_for_coaching") is True
            and report.get("duration_s", 0) >= 30)


def _approved_plan(plan: object) -> bool:
    if not isinstance(plan, dict) or plan.get("drill") not in {"clicking", "tracking", "switching"}:
        return False
    if not isinstance(plan.get("cue"), str) or not plan["cue"].strip():
        return False
    parameters = plan.get("parameters")
    if not isinstance(parameters, dict) or set(parameters) != {"duration_s", "target_scale", "speed_scale"}:
        return False
    return all(type(parameters[key]) in (int, float) and math.isfinite(parameters[key])
               and low <= parameters[key] <= high for key, low, high in (
                   ("duration_s", 30, 90), ("target_scale", .5, 2), ("speed_scale", .5, 2)))


def next_training_action(storage, settings: dict | None = None) -> dict:
    """Resume the saved baseline -> approved practice -> retest loop, without AI.

    Baseline rows are retained separately from recent activity so playing more
    than 100 free rounds cannot erase onboarding. All state remains in sessions
    and coaching; this read has no side effects and survives process restarts.
    """
    if settings is not None and not isinstance(settings, dict):
        raise ValueError("Training settings must be an object")
    recent = [item for item in storage.list_sessions(limit=100) if _kind(item) != "sensitivity"]
    baselines = _baseline_reports(storage)
    reports = {item["record_id"]: item for item in [*baselines, *recent]}
    records = {}

    def record(identifier):
        if identifier not in records:
            records[identifier] = storage.get_session(identifier)
        return records[identifier]

    def report(identifier):
        if identifier not in reports:
            reports[identifier] = storage.get_report(identifier)
        return reports[identifier]

    groups = {}
    profiles = {}
    for item in baselines:
        if not _usable(item):
            continue
        cycle = item.get("training_context", {}).get("cycle_id") or item["record_id"]
        if _mode(item) in groups.get(cycle, {}):
            continue
        raw = record(item["record_id"])
        if not raw.get("completed") or not _profile_matches(raw, settings):
            continue
        profile = {key: raw["settings"][key] for key in ("sensitivity_deg_per_count", "fov")}
        if cycle in profiles and profiles[cycle] != profile:
            continue
        profiles.setdefault(cycle, profile)
        groups.setdefault(cycle, {}).setdefault(_mode(raw), item)
        if all(mode in groups[cycle] for mode in BASELINE_MODES):
            break
    complete = [(cycle, modes) for cycle, modes in groups.items() if all(mode in modes for mode in BASELINE_MODES)]
    if complete:
        baseline_cycle, baseline_modes = max(complete, key=lambda group: max(item["started_at"] for item in group[1].values()))
    elif groups:
        # Preserve a legacy three-mode baseline when a later one-off measurement
        # has its own practice cycle; never merge unrelated partial cycles.
        baseline_cycle, baseline_modes = max(groups.items(), key=lambda group: (
            len(group[1]), max(item["started_at"] for item in group[1].values())))
    else:
        baseline_cycle, baseline_modes = "", {}
    missing = [mode for mode in BASELINE_MODES if mode not in baseline_modes]
    result = {"action": "baseline", "baseline_complete": not missing, "missing_modes": missing,
              "cycle_id": baseline_cycle, "record_id": "", "source_coaching_record_id": "",
              "recommendation": None, "baseline_record": None, "record": None, "report": None,
              "remaining_rounds": 0, "settings": None, "drill": None, "mode": None,
              "reason": "Complete the remaining baseline measurements."}
    if missing:
        return result

    latest = next((item for item in recent if _profile_matches(record(item["record_id"]), settings)
                   and record(item["record_id"]).get("completed")), None)
    if latest is None:
        latest = max(baseline_modes.values(), key=lambda item: item["started_at"])
    latest_id = latest["record_id"]
    latest_record = record(latest_id)
    result.update(action="review", cycle_id="", record_id=latest_id, record=_snapshot(latest_record),
                  report=copy.deepcopy({key: latest[key] for key in (
                      "record_id", "drill", "started_at", "duration_s", "benchmark_key", "valid", "quality", "metrics",
                      "training_context", "tracking_motion") if key in latest}),
                  reason="Review your latest completed round before choosing the next adjustment.")
    training = latest.get("training_context", {})
    latest_plan = storage.get_coaching(latest_id)
    if _kind(latest) == "practice" and training.get("cycle_id") and not _approved_plan(latest_plan):
        source_id, baseline_id = training.get("source_coaching_record_id"), training.get("baseline_record_id")
        if not source_id or not baseline_id:
            return result
        baseline = record(baseline_id)
        plan = storage.get_coaching(source_id)
        if (not _approved_plan(plan) or not _usable(report(baseline_id))
                or plan["drill"] != baseline["drill"] or _mode(baseline) != _mode(latest_record)
                or not _profile_matches(baseline, settings)):
            return result
        prescribed = _clean_settings(baseline["settings"]) | plan["parameters"]
        if _clean_settings(latest_record["settings"]) != prescribed:
            result["reason"] = "The latest practice used different settings; review it before continuing the plan."
            return result
        counted = []
        for item in recent:
            context = item.get("training_context", {})
            if (_kind(item) != "practice" or context.get("cycle_id") != training["cycle_id"]
                    or context.get("source_coaching_record_id") != source_id
                    or context.get("baseline_record_id") != baseline_id or not _usable(item)):
                continue
            raw = record(item["record_id"])
            if (raw.get("completed") and raw["drill"] == baseline["drill"]
                    and raw.get("schema_version") == baseline.get("schema_version")
                    and raw.get("drill_version", 1) == baseline.get("drill_version", 1)
                    and _clean_settings(raw["settings"]) == prescribed):
                counted.append(item)
        remaining = max(0, 2 - len(counted))
        result.update(action="continue_practice" if remaining else "retest", cycle_id=training["cycle_id"],
                      source_coaching_record_id=source_id, recommendation=copy.deepcopy(plan),
                      baseline_record=_snapshot(baseline), remaining_rounds=remaining,
                      settings=prescribed if remaining else _clean_settings(baseline["settings"]),
                      drill=baseline["drill"], mode=_mode(baseline),
                      reason="Finish the approved practice block." if remaining else "Measure the adjustment at the original baseline settings.")
        return result

    plan = latest_plan
    if not _approved_plan(plan):
        return result
    target = plan["drill"]
    selected = None
    if latest["drill"] == target and _kind(latest) != "practice" and _usable(latest):
        selected = latest_record
    elif latest["drill"] == target and training.get("baseline_record_id"):
        linked = record(training["baseline_record_id"])
        if (_usable(report(linked["id"])) and _mode(linked) == _mode(latest_record)
                and _profile_matches(linked, settings)):
            selected = linked
    if selected is None:
        # Cross-drill tracking has two real modes. The cited saved measurement
        # identifies the mode; absence of that evidence must not imply smooth.
        for evidence_id in plan.get("evidence_ids", []):
            identifier = evidence_id.split(":", 1)[0] if isinstance(evidence_id, str) else ""
            try:
                candidate_report = report(identifier)
            except (KeyError, ValueError):
                continue
            if (candidate_report["drill"] == target and _kind(candidate_report) not in {"practice", "sensitivity"}
                    and candidate_report["started_at"] <= latest["started_at"] and _usable(candidate_report)):
                candidate = record(identifier)
                if candidate.get("completed") and _profile_matches(candidate, settings):
                    selected = candidate
                    break
        if selected is None and target != "tracking":
            selected = record(baseline_modes[target]["record_id"])
    if selected is None:
        result["reason"] = "The saved advice needs to identify which tracking measurement the next drill uses."
        return result
    result.update(action="coached_practice", source_coaching_record_id=latest_id,
                  recommendation=copy.deepcopy(plan), baseline_record=_snapshot(selected), remaining_rounds=2,
                  settings=_clean_settings(selected["settings"]) | plan["parameters"],
                  drill=target, mode=_mode(selected), reason="Practice the adjustment approved for your latest results.")
    return result
