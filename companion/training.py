"""One persisted comparison and coaching memory shared by the UI and the coach."""
from __future__ import annotations

import math


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
    seen_drills = {report["drill"], *(item["drill"] for item in history)}
    # Tagged baselines use their cycle, not nearby free practice. Legacy rounds
    # retain the earlier short-window behavior until they acquire explicit links.
    for summary in storage.list_sessions(limit=100):
        record_id = summary["record_id"]
        if record_id in selected or summary.get("drill") in seen_drills or _kind(summary) in {"practice", "sensitivity"}:
            continue
        age = report.get("started_at", 0) - summary.get("started_at", 0)
        if not 0 <= age <= 15 * 60:
            continue
        if training.get("cycle_id") and summary.get("training_context", {}).get("cycle_id") != training["cycle_id"]:
            continue
        other = storage.get_report(record_id)
        history.append(other)
        selected.add(record_id)
        seen_drills.add(other["drill"])
        if len(seen_drills) == 3:
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
