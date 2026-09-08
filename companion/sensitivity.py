"""Controlled sensitivity screening; recommendations remain provisional and opt-in."""
from __future__ import annotations

import copy
import json
import math
import random
import secrets
import time
from statistics import mean
from uuid import uuid4

from .metrics import benchmark_key, canonicalize_json, validate_id, validate_record

MEASURED_SECONDS = 20
SETTLE_SECONDS = 5
MIN_SENSITIVITY = .001
MAX_SENSITIVITY = .5
METRICS = ("accuracy_pct", "acquisition_ms", "click_hits_per_s",
           "tracking_error_deg", "time_on_target_pct")


def _number(value, name: str, minimum: float, maximum: float) -> float:
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not minimum <= value <= maximum):
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return float(value)


def create_experiment(settings: dict) -> dict:
    """Two matched repeats of clicking and tracking at each candidate setting.

    Reversing the entire second pass puts every block at the same average order
    position. Each drill/repeat uses a fresh seed shared by all sensitivities.
    This reduces simple order effects; it does not eliminate adaptation bias.
    """
    if not isinstance(settings, dict) or len(settings) > 64:
        raise ValueError("settings must be an object with at most 64 properties")
    base = canonicalize_json(settings)
    sensitivity = _number(base.get("sensitivity_deg_per_count", .07),
                          "sensitivity_deg_per_count", MIN_SENSITIVITY, MAX_SENSITIVITY)
    for field, default, low, high in (("fov", 103, 60, 130),
                                    ("target_scale", 1, .5, 2), ("speed_scale", 1, .4, 2)):
        base[field] = _number(base.get(field, default), field, low, high)
    base.update(sensitivity_deg_per_count=sensitivity, fov_axis="horizontal", drill_version=1)
    # Give the real current setting precedence when clamping creates a duplicate.
    chosen = {sensitivity: {"id": "current", "sensitivity_deg_per_count": sensitivity, "multiplier": 1.0}}
    for name, multiplier in (("lower", .8), ("higher", 1.2)):
        value = round(max(MIN_SENSITIVITY, min(MAX_SENSITIVITY, sensitivity * multiplier)), 9)
        if not any(math.isclose(value, existing, rel_tol=1e-9) for existing in chosen):
            chosen[value] = {"id": name, "sensitivity_deg_per_count": value,
                             "multiplier": value / sensitivity}
    candidates = sorted(chosen.values(), key=lambda item: item["sensitivity_deg_per_count"])
    order = [(candidate["id"], drill) for candidate in candidates for drill in ("clicking", "tracking")]
    random.SystemRandom().shuffle(order)
    values = {candidate["id"]: candidate["sensitivity_deg_per_count"] for candidate in candidates}
    blocks = []
    used_seeds = set()
    for repetition, pairs in enumerate((order, list(reversed(order)))):
        seeds = {}
        for drill in ("clicking", "tracking"):
            seed = secrets.randbelow(2**31)
            while seed in used_seeds:
                seed = (seed + 1) % 2**31
            used_seeds.add(seed)
            seeds[drill] = seed
        for candidate_id, drill in pairs:
            config = copy.deepcopy(base)
            config.update(sensitivity_deg_per_count=values[candidate_id],
                          duration_s=MEASURED_SECONDS, seed=seeds[drill])
            blocks.append({"index": len(blocks), "candidate_id": candidate_id, "drill": drill,
                           "repetition": repetition, "settings": config, "settle_s": SETTLE_SECONDS})
    return {"id": "sens-" + uuid4().hex, "protocol_version": 1, "status": "collecting",
            "created_at": time.time(), "base_settings": base, "candidates": candidates,
            "blocks": blocks, "record_ids": [], "results": []}


def add_result(experiment: dict, record: dict, report: dict) -> dict:
    """Attach only the next completed, correctly configured measured round."""
    validate_record(record)
    updated = copy.deepcopy(experiment)
    validate_id(updated["id"])
    context = record.get("sensitivity_context", {})
    if not isinstance(context, dict) or context.get("experiment_id") != updated["id"]:
        raise ValueError("Round does not belong to this sensitivity experiment")
    index = context.get("block_index")
    if (type(index) not in (int, float) or not math.isfinite(index)
            or int(index) != index or not 0 <= index < len(updated["blocks"])):
        raise ValueError("Invalid sensitivity block index")
    index = int(index)
    block = updated["blocks"][index]
    if context.get("candidate_id") != block["candidate_id"]:
        raise ValueError("Sensitivity candidate does not match the measured block")
    training = record.get("training_context", {})
    if not isinstance(training, dict) or training.get("kind") != "sensitivity" or training.get("cycle_id") != updated["id"]:
        raise ValueError("Round must be tagged as sensitivity screening")
    if not record["completed"] or not MEASURED_SECONDS - .05 <= record["duration_s"] <= MEASURED_SECONDS + 1:
        raise ValueError("Finish the entire measured round before continuing")
    if (record["drill"] != block["drill"] or
            canonicalize_json(record["settings"]) != canonicalize_json(block["settings"])):
        raise ValueError("Round settings do not match the sensitivity block")
    if (not isinstance(report, dict) or report.get("record_id") != record["id"]
            or report.get("drill") != record["drill"]
            or report.get("benchmark_key") != benchmark_key(record)
            or report.get("duration_s") != record["duration_s"]):
        raise ValueError("Measurement report does not match the submitted round")
    # Network retries may re-submit one assignment, but cannot reuse a round for
    # a different block or skip the next block in the balanced schedule.
    if record["id"] in updated["record_ids"]:
        if index < len(updated["record_ids"]) and updated["record_ids"][index] == record["id"]:
            return updated
        raise ValueError("A measured round cannot be reused for another block")
    if updated["status"] != "collecting" or index != len(updated["record_ids"]):
        raise ValueError("Complete sensitivity blocks in their planned order")
    updated["record_ids"].append(record["id"])
    updated["results"].append({"block_index": index, "record_id": record["id"],
                               "benchmark_key": report["benchmark_key"],
                               "ended_at": record["started_at"] + record["duration_s"]})
    if len(updated["record_ids"]) == len(updated["blocks"]):
        updated["status"] = "complete"
        updated["completed_at"] = max(result["ended_at"] for result in updated["results"]
                                      if "ended_at" in result)
    return updated


def _metric(report: dict, name: str) -> float | None:
    value = report.get("metrics", {}).get(name, {}).get("value")
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _round_values(clicking: dict, tracking: dict) -> dict:
    hits = _metric(clicking, "hits")
    return {"accuracy_pct": _metric(clicking, "accuracy_pct"),
            "acquisition_ms": _metric(clicking, "acquisition_ms"),
            "click_hits_per_s": hits / clicking["duration_s"] if hits is not None else None,
            "tracking_error_deg": _metric(tracking, "tracking_error_deg"),
            "time_on_target_pct": _metric(tracking, "time_on_target_pct")}


def _quality_problems(report: dict) -> list[str]:
    quality = report.get("quality", {})
    problems = list(quality.get("flags", []))
    if not report.get("valid") or not quality.get("usable_for_coaching"):
        problems.append("insufficient_measurement_quality")
    if report["drill"] == "tracking":
        observed = next((item.get("data", {}).get("observed_s", 0)
                         for item in report.get("evidence", [])
                         if item.get("id", "").endswith(":aim-summary")), 0)
        if not isinstance(observed, (float, int)) or observed < 15:
            problems.append("low_tracking_engagement")
    elif (_metric(report, "shots") or 0) < 10 or (_metric(report, "hits") or 0) < 5:
        problems.append("too_few_clicks")
    return sorted(set(problems))


def _paired_gain(candidate: dict, current: dict) -> float:
    # Equal emphasis on clicking speed and tracking. Accuracy is an additional
    # guardrail, so a burst of inaccurate shooting cannot win on speed alone.
    click = .5 * (math.log(candidate["click_hits_per_s"] / current["click_hits_per_s"])
                  + math.log(current["acquisition_ms"] / candidate["acquisition_ms"]))
    track = .5 * (math.log(max(.05, current["tracking_error_deg"]) /
                           max(.05, candidate["tracking_error_deg"]))
                  + math.log((candidate["time_on_target_pct"] + 5) /
                             (current["time_on_target_pct"] + 5)))
    return .5 * (click + track)


def _regressions(candidate: dict, current: dict) -> list[str]:
    reasons = []
    if candidate["accuracy_pct"] < current["accuracy_pct"] - 2:
        reasons.append("accuracy_dropped")
    if candidate["acquisition_ms"] > current["acquisition_ms"] * 1.05:
        reasons.append("acquisition_slower")
    if candidate["click_hits_per_s"] < current["click_hits_per_s"] * .95:
        reasons.append("fewer_hits_per_second")
    if candidate["tracking_error_deg"] > current["tracking_error_deg"] * 1.05 + .05:
        reasons.append("tracking_error_increased")
    if candidate["time_on_target_pct"] < current["time_on_target_pct"] - 3:
        reasons.append("time_on_target_dropped")
    return reasons


def _acquisition_change(candidate: dict, current: dict) -> str:
    delta = candidate["acquisition_ms"] - current["acquisition_ms"]
    if abs(delta) < .5:
        return f"similar acquisition time ({candidate['acquisition_ms']:.0f} vs {current['acquisition_ms']:.0f} ms)"
    return f"{abs(delta):.0f} ms {'slower' if delta > 0 else 'faster'} acquisition"


def _accuracy_change(candidate: dict, current: dict) -> str:
    delta = candidate["accuracy_pct"] - current["accuracy_pct"]
    if abs(delta) < .05:
        return "the same accuracy at the shown precision"
    return f"{abs(delta):.1f} percentage points {'higher' if delta > 0 else 'lower'} accuracy"


def _repeat_comparison(candidate: dict, current: dict, repetition: int) -> str:
    text = (f"Repeat {repetition + 1}: {_acquisition_change(candidate, current)}; "
            f"accuracy {candidate['accuracy_pct']:.1f}% vs {current['accuracy_pct']:.1f}%")
    regressions = _regressions(candidate, current)
    if "tracking_error_increased" in regressions:
        text += f"; tracking error {candidate['tracking_error_deg']:.2f}° vs {current['tracking_error_deg']:.2f}°"
    if "time_on_target_dropped" in regressions:
        text += f"; time on target {candidate['time_on_target_pct']:.1f}% vs {current['time_on_target_pct']:.1f}%"
    if "fewer_hits_per_second" in regressions:
        text += f"; hits/s {candidate['click_hits_per_s']:.2f} vs {current['click_hits_per_s']:.2f}"
    return text + "."


def _quality_description(report: dict) -> str:
    """Describe missing evidence without guessing why the player produced it."""
    flags = set(_quality_problems(report))
    aim = next((item.get("data", {}) for item in report.get("evidence", [])
                if item.get("id", "").endswith(":aim-summary")), {})
    details = []
    if "low_tracking_engagement" in flags:
        observed = aim.get("observed_s", 0)
        details.append(f"only {observed:.1f} s of usable engaged tracking")
    if flags & {"low_attempts", "too_few_clicks"}:
        details.append(f"too few clicks ({_metric(report, 'shots') or 0:.0f} shots, {_metric(report, 'hits') or 0:.0f} hits)")
    if "low_sample_cadence" in flags:
        details.append(f"recording cadence of {report.get('quality', {}).get('sample_hz', 0):.1f} samples/s")
    if "sample_gaps" in flags:
        details.append("gaps in the aim recording")
    if "low_sample_coverage" in flags:
        details.append("aim recording missing for part of the round")
    if "short_run" in flags:
        details.append(f"only {report.get('duration_s', 0):.1f} s measured")
    if "incomplete_run" in flags:
        details.append("an unfinished round")
    if not details and flags:
        details.append("insufficient usable aim measurements")
    return ", ".join(details)


def _add_comparison_summaries(candidates: list[dict], current: dict, grouped: dict) -> None:
    for candidate in candidates:
        problems = []
        for repetition, drills in grouped[candidate["id"]].items():
            for drill, report in drills.items():
                detail = _quality_description(report)
                if detail:
                    problems.append(f"{drill.capitalize()} repeat {repetition + 1}: {detail}.")
        if problems:
            candidate["comparison_summary"] = " ".join(problems)
            continue
        if candidate["quality_flags"]:
            candidate["comparison_summary"] = "A required measurement is missing, so this setting cannot be compared reliably with current."
            continue
        if current["quality_flags"]:
            candidate["comparison_summary"] = "The current-setting rounds do not provide a reliable reference for this comparison."
            continue
        values, baseline = candidate["metrics"], current["metrics"]
        if candidate is current:
            candidate["comparison_summary"] = (
                f"Reference across both repeats: {values['accuracy_pct']:.1f}% accuracy, "
                f"{values['acquisition_ms']:.0f} ms acquisition and {values['click_hits_per_s']:.2f} hits/s. "
                f"Tracking: {values['time_on_target_pct']:.1f}% on target, {values['tracking_error_deg']:.2f}° error.")
            continue
        candidate["comparison_summary"] = (
            f"Compared with current, averaged {_acquisition_change(values, baseline)} and "
            f"{_accuracy_change(values, baseline)}; hits/s {values['click_hits_per_s']:.2f} vs {baseline['click_hits_per_s']:.2f}. "
            + " ".join(_repeat_comparison(test["metrics"], reference["metrics"], test["repetition"])
                       for test, reference in zip(candidate["repeats"], current["repeats"]))
            + f" Tracking average: {values['time_on_target_pct']:.1f}% vs {baseline['time_on_target_pct']:.1f}% on target, "
              f"{values['tracking_error_deg']:.2f}° vs {baseline['tracking_error_deg']:.2f}° error.")


def _current_choice_explanation(candidates: list[dict], current: dict) -> str:
    reasons = []
    for candidate in candidates:
        if candidate is current:
            continue
        values, baseline = candidate["metrics"], current["metrics"]
        reason = f"{candidate['id'].capitalize()} averaged {_acquisition_change(values, baseline)} and {_accuracy_change(values, baseline)}."
        # Surface a masked tradeoff: the aggregate may look quicker even though
        # one matched repeat lost the benefit or sacrificed another measurement.
        if values["acquisition_ms"] < baseline["acquisition_ms"] and not candidate["strong_evidence"]:
            weakest = min(range(2), key=lambda index: candidate["repeat_consistency"]["paired_gains"][index])
            reason += " " + _repeat_comparison(candidate["repeats"][weakest]["metrics"],
                                                 current["repeats"][weakest]["metrics"], weakest)
        elif candidate["regression_flags"] and not any(flag in candidate["regression_flags"]
                for flag in ("accuracy_dropped", "acquisition_slower", "fewer_hits_per_second")):
            weakest = min(range(2), key=lambda index: candidate["repeat_consistency"]["paired_gains"][index])
            reason += " " + _repeat_comparison(candidate["repeats"][weakest]["metrics"],
                                                 current["repeats"][weakest]["metrics"], weakest)
        reasons.append(reason)
    return ("The short screening has no clear winner; current remains the conservative starting point. "
            + " ".join(reasons) + " Another measured setting can still be tried with low confidence.")


def analyze_experiment(experiment: dict, reports: list[dict]) -> dict:
    """Separate adequately measured trial options from a consistent screen winner.

    Thresholds are conservative product heuristics, not significance tests.
    Raw metric differences and repeat consistency are supplied for AI review.
    """
    records = experiment["record_ids"]
    by_id = {}
    for report in reports:
        record_id = report.get("record_id")
        if record_id in by_id:
            raise ValueError("Duplicate sensitivity measurement report")
        if record_id not in records:
            raise ValueError("Report is not part of this sensitivity experiment")
        by_id[record_id] = report
    candidates = [dict(copy.deepcopy(candidate), metrics={}, repeats=[], eligible=False, strong_evidence=False,
                       repeat_consistency={"paired_gains": [], "direction_agrees": False},
                       quality_flags=[], regression_flags=[], tradeoff_flags=[], comparison_summary="Awaiting the complete comparison.")
                  for candidate in experiment["candidates"]]
    analysis = {"experiment_id": experiment["id"], "status": "incomplete", "confidence": "low",
                "candidates": candidates, "completed_blocks": len(records), "total_blocks": len(experiment["blocks"]),
                "allowed_candidate_ids": ["current"], "recommended_candidate_id": "current", "evidence": [],
                "strong_candidate_ids": [],
                "explanation": "Finish every measured block before comparing sensitivities. Keep your current setting for now."}
    protocol_id = experiment["id"] + ":protocol"
    analysis["evidence"].append({"id": protocol_id, "summary": "Controlled sensitivity screening protocol",
                                "data": {"completed_blocks": len(records), "total_blocks": len(experiment["blocks"]),
                                         "measured_seconds": MEASURED_SECONDS, "unscored_adaptation_seconds": SETTLE_SECONDS,
                                         "repeats_per_drill": 2, "matched_seeds": True,
                                         "second_pass_order": "reversed", "interpretation": "short screening, not an optimum"}})
    if (experiment.get("status") != "complete" or len(records) != len(experiment["blocks"])
            or set(by_id) != set(records)):
        return analysis
    analysis["screen_completed_at"] = max(report["started_at"] + report["duration_s"] for report in by_id.values())
    grouped = {candidate["id"]: {0: {}, 1: {}} for candidate in candidates}
    for index, block in enumerate(experiment["blocks"]):
        report = by_id[records[index]]
        expected = {"schema_version": 1, "settings": block["settings"], "drill": block["drill"]}
        if report.get("drill") != block["drill"] or report.get("benchmark_key") != benchmark_key(expected):
            raise ValueError("Sensitivity report does not match its assigned block")
        grouped[block["candidate_id"]][block["repetition"]][block["drill"]] = report
    for candidate in candidates:
        pairs = grouped[candidate["id"]]
        candidate["record_ids"] = [report["record_id"] for drills in pairs.values() for report in drills.values()]
        candidate["quality_flags"] = sorted({flag for drills in pairs.values() for report in drills.values()
                                             for flag in _quality_problems(report)})
        for repetition, drills in pairs.items():
            values = _round_values(drills["clicking"], drills["tracking"])
            if (any(value is None for value in values.values())
                    or any((values[name] or 0) <= 0 for name in ("acquisition_ms", "click_hits_per_s"))):
                candidate["quality_flags"].append("missing_performance_metrics")
            candidate["repeats"].append({"repetition": repetition, "metrics": values})
        candidate["metrics"] = {name: round(mean(values), 4) if len(values) == 2 else None
                                for name in METRICS
                                for values in [[item["metrics"][name] for item in candidate["repeats"]
                                                if item["metrics"][name] is not None]]}
        analysis["evidence"].append({"id": experiment["id"] + ":" + candidate["id"],
                                     "summary": f"Measured {candidate['id']} sensitivity across two clicking and tracking repeats",
                                     "data": {"sensitivity_deg_per_count": candidate["sensitivity_deg_per_count"],
                                              "record_ids": candidate["record_ids"], "metrics": candidate["metrics"],
                                              "repeats": copy.deepcopy(candidate["repeats"]),
                                              "quality_flags": candidate["quality_flags"]}})
    analysis["status"] = "inconclusive"
    current = next(candidate for candidate in candidates if candidate["id"] == "current")
    _add_comparison_summaries(candidates, current, grouped)
    if any(candidate["quality_flags"] for candidate in candidates):
        details = " ".join(f"{candidate['id'].capitalize()}: {candidate['comparison_summary']}"
                           for candidate in candidates if candidate["quality_flags"])
        analysis["explanation"] = "Keep your current sensitivity. " + details + " These measurements cannot support a fair comparison."
        return analysis
    current["eligible"] = True
    current["repeat_consistency"] = {"paired_gains": [0.0, 0.0], "direction_agrees": True}
    analysis["allowed_candidate_ids"] = ["current"] + [candidate["id"] for candidate in candidates if candidate is not current]
    for candidate in candidates:
        if candidate is current:
            continue
        gains = [_paired_gain(test["metrics"], baseline["metrics"])
                 for test, baseline in zip(candidate["repeats"], current["repeats"])]
        regressions = sorted({flag for test, baseline in zip(candidate["repeats"], current["repeats"])
                              for flag in _regressions(test["metrics"], baseline["metrics"])})
        candidate["repeat_consistency"] = {"paired_gains": [round(gain, 4) for gain in gains],
                                           "direction_agrees": gains[0] * gains[1] > 0}
        candidate["regression_flags"] = regressions
        candidate["tradeoff_flags"] = list(regressions)
        candidate["eligible"] = True  # Measured tradeoffs inform judgment; they do not ban a trial.
        candidate["strong_evidence"] = min(gains) >= .03 and mean(gains) >= .05 and not regressions
    analysis["explanation"] = _current_choice_explanation(candidates, current)
    eligible = [candidate for candidate in candidates if candidate["strong_evidence"] and candidate is not current]
    if eligible:
        eligible.sort(key=lambda candidate: mean(candidate["repeat_consistency"]["paired_gains"]), reverse=True)
        # A tiny score difference between alternatives is not a reliable choice.
        if len(eligible) > 1 and (mean(eligible[0]["repeat_consistency"]["paired_gains"])
                                 - mean(eligible[1]["repeat_consistency"]["paired_gains"])) < .03:
            analysis["explanation"] += " Both alternatives look promising, but this short screening cannot distinguish them clearly."
        else:
            winner = eligible[0]
            analysis.update(status="provisional", confidence="medium",
                            strong_candidate_ids=[winner["id"]], recommended_candidate_id=winner["id"],
                            explanation=f"The {winner['id']} setting performed better across both repeats without a material speed, accuracy, or tracking tradeoff. Try it provisionally, then retest next session; this is not a proven optimum.")
    analysis["evidence"].append({"id": experiment["id"] + ":comparison", "summary": "Paired repeat consistency and tradeoffs against current sensitivity",
                                 "data": {candidate["id"]: {"repeat_consistency": candidate["repeat_consistency"],
                                                            "regression_flags": candidate["regression_flags"],
                                                            "eligible": candidate["eligible"],
                                                            "strong_evidence": candidate["strong_evidence"]} for candidate in candidates}})
    return analysis


def analyze_follow_up(experiment: dict, rounds: list[dict]) -> dict:
    """Compare later normal rounds without treating an observational pair as proof."""
    result = {"status": "none", "record_ids": [], "comparisons": [], "evidence": [],
              "summary": "No matched post-screening sensitivity comparison is available yet."}
    cutoff = experiment.get("completed_at")
    if (experiment.get("status") != "complete" or type(cutoff) not in (int, float)
            or not math.isfinite(cutoff)):
        return result
    grouped = {}
    for item in rounds:
        record, report = item.get("record", {}), item.get("report", {})
        try:
            validate_record(record)
        except (ValueError, TypeError):
            continue
        if (not record["completed"] or record["started_at"] < cutoff
                or record["id"] in experiment["record_ids"] or record.get("sensitivity_context")
                or record.get("training_context", {}).get("kind") == "sensitivity"):
            continue
        if (report.get("record_id") != record["id"] or report.get("drill") != record["drill"]
                or report.get("benchmark_key") != benchmark_key(record)
                or report.get("started_at") != record["started_at"]
                or report.get("duration_s") != record["duration_s"] or _quality_problems(report)):
            continue
        sensitivity = record["settings"]["sensitivity_deg_per_count"]
        candidate = next((candidate for candidate in experiment["candidates"]
                          if math.isclose(sensitivity, candidate["sensitivity_deg_per_count"], rel_tol=1e-9)), None)
        if candidate is None:
            continue
        controlled = {key: value for key, value in record["settings"].items()
                      if key not in {"seed", "sensitivity_deg_per_count"}}
        requested_duration = controlled.get("duration_s")
        if (type(requested_duration) not in (int, float)
                or not requested_duration - .05 <= record["duration_s"] <= requested_duration + 1):
            continue
        comparison_key = json.dumps(canonicalize_json({"settings": controlled, "drill": record["drill"],
                                      "schema_version": record["schema_version"],
                                      "drill_version": record.get("drill_version", 1),
                                      "metric_version": report.get("metric_version")}), sort_keys=True)
        grouped.setdefault(comparison_key, []).append({"record": record, "report": report, "candidate_id": candidate["id"]})
    pairs = []
    for values in grouped.values():
        values.sort(key=lambda item: (item["record"]["started_at"], item["record"]["id"]), reverse=True)
        selected_kinds = set()
        for after in values:
            kind = after["record"].get("training_context", {}).get("kind", "free")
            if kind in selected_kinds:
                continue
            before = next((item for item in values if item["candidate_id"] != after["candidate_id"]
                           and item["record"]["started_at"] + item["record"]["duration_s"] <= after["record"]["started_at"]), None)
            if before:
                pairs.append((before, after))
                # Preserve the latest free comparison when prescribed practice
                # follows it; practice is explicitly labelled as another influence.
                selected_kinds.add(kind)
    pairs.sort(key=lambda pair: pair[1]["record"]["started_at"], reverse=True)
    limitation = ("One observational pair: practice, order, and adaptation may explain some of the change. "
                  "This does not establish that sensitivity caused it or that improvement is retained.")
    for before, after in pairs[:3]:
        deltas = {}
        for name in ("accuracy_pct", "acquisition_ms", "hit_interval_ms", "overshoot_pct", "correction_ms",
                     "tracking_error_deg", "time_on_target_pct"):
            previous, current = _metric(before["report"], name), _metric(after["report"], name)
            previous_unit = before["report"].get("metrics", {}).get(name, {}).get("unit")
            unit = after["report"].get("metrics", {}).get(name, {}).get("unit")
            if previous is not None and current is not None and previous_unit == unit:
                deltas[name] = {"previous": previous, "current": current, "delta": round(current - previous, 4), "unit": unit}
        if not deltas:
            continue
        details = []
        fields = (("time_on_target_pct", "time on target", "%", 1), ("tracking_error_deg", "tracking error", "°", 2)) if after["record"]["drill"] == "tracking" else (
                  ("accuracy_pct", "accuracy", "%", 1), ("acquisition_ms", "acquisition", " ms", 0), ("overshoot_pct", "overshoots", "%", 1))
        for name, label, suffix, precision in fields:
            if name in deltas:
                delta = deltas[name]
                details.append(f"{label} {delta['previous']:.{precision}f}{suffix} → {delta['current']:.{precision}f}{suffix}")
        if not details:
            continue
        summary = (f"Later {after['record']['drill']} comparison, {before['candidate_id']} → {after['candidate_id']}: "
                   + "; ".join(details) + ".")
        identifier = f"{experiment['id']}:follow-up:{before['record']['id']}:{after['record']['id']}"
        comparison = {"evidence_id": identifier, "before_record_id": before["record"]["id"],
                      "after_record_id": after["record"]["id"], "before_candidate_id": before["candidate_id"],
                      "after_candidate_id": after["candidate_id"], "drill": after["record"]["drill"],
                      "duration_s": after["record"]["settings"]["duration_s"], "settings_matched": True,
                      "matched_except": ["seed", "sensitivity_deg_per_count"], "metrics": deltas,
                      "before_started_at": before["record"]["started_at"], "after_started_at": after["record"]["started_at"],
                      "before_training_kind": before["record"].get("training_context", {}).get("kind", "free"),
                      "after_training_kind": after["record"].get("training_context", {}).get("kind", "free"),
                      "summary": summary, "limitation": limitation}
        result["comparisons"].append(comparison)
        result["evidence"].append({"id": identifier, "summary": summary, "data": copy.deepcopy(comparison)})
    if result["comparisons"]:
        result["status"] = "available"
        result["record_ids"] = list(dict.fromkeys(record_id for pair in result["comparisons"]
                                                 for record_id in (pair["before_record_id"], pair["after_record_id"])))
        result["summary"] = " ".join(pair["summary"] for pair in result["comparisons"]) + " " + limitation
        if len(result["comparisons"]) > 1:
            result["summary"] += " Comparisons sharing a round are not independent repeats."
    return result


def add_follow_up(analysis: dict, experiment: dict, rounds: list[dict]) -> dict:
    """Attach new evidence while preserving the original screen and its scoring."""
    updated = copy.deepcopy(analysis)
    source = dict(experiment)
    if "screen_completed_at" in analysis:
        source["completed_at"] = analysis["screen_completed_at"]
    follow_up = analyze_follow_up(source, rounds)
    old_ids = {item["id"] for item in updated.get("follow_up", {}).get("evidence", [])}
    updated["evidence"] = [item for item in updated.get("evidence", []) if item["id"] not in old_ids]
    updated["follow_up"] = follow_up
    updated["evidence"].extend(follow_up["evidence"])
    return updated
