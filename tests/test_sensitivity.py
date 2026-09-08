import copy
import unittest

from companion.metrics import analyze_record
from companion.sensitivity import add_follow_up, add_result, analyze_experiment, analyze_follow_up, create_experiment


SETTINGS = {"sensitivity_deg_per_count": .07, "fov": 103, "target_scale": 1,
            "speed_scale": 1, "duration_s": 45, "dpi": 800}


def measured_round(experiment, index):
    block = experiment["blocks"][index]
    tracking = block["drill"] == "tracking"
    record = {"schema_version": 1, "id": "measurement-" + str(index), "drill": block["drill"],
              "started_at": 1000 + 25 * index, "duration_s": 20, "completed": True,
              "settings": copy.deepcopy(block["settings"]), "samples": [], "shots": [], "events": [],
              "sensitivity_context": {"experiment_id": experiment["id"],
                                      "candidate_id": block["candidate_id"], "block_index": index},
              "training_context": {"kind": "sensitivity", "cycle_id": experiment["id"]}}
    for i in range(401):
        t = i * .05
        target_id = "target" if tracking else "target-" + str(min(19, int(t)))
        record["samples"].append({"t": t, "yaw": 0, "pitch": 0, "target_id": target_id,
                                   "target_yaw": 0, "target_pitch": 0, "target_radius": 1,
                                   "on_target": True, "engaged": True, "frame_ms": 4})
    if not tracking:
        for i in range(20):
            target_id = "target-" + str(i)
            record["shots"].append({"t": i + .5, "hit": True, "target_id": target_id, "error_deg": 0})
            record["events"].append({"t": i, "type": "spawn", "target_id": target_id,
                                      "yaw": 0, "pitch": 0, "radius": 1})
    return record


def complete_experiment(performance=None):
    experiment = create_experiment(SETTINGS)
    reports = []
    for index, block in enumerate(experiment["blocks"]):
        record = measured_round(experiment, index)
        report = analyze_record(record)
        # Controlled reports test the ranking independently from telemetry math,
        # which has its own geometric tests. Assignment uses a real valid record.
        values = {"accuracy_pct": 95, "acquisition_ms": 600, "hits": 30,
                  "tracking_error_deg": 2, "time_on_target_pct": 60, "shots": 32}
        if performance:
            values.update(performance(block["candidate_id"], block["repetition"], block["drill"]))
        for name, value in values.items():
            report["metrics"][name]["value"] = value
        experiment = add_result(experiment, record, report)
        reports.append(report)
    return experiment, reports


def normal_round(experiment, candidate_id, record_id, started_at, metric_values=None):
    index = next(block["index"] for block in experiment["blocks"]
                 if block["candidate_id"] == candidate_id and block["drill"] == "clicking")
    record = measured_round(experiment, index)
    record.update(id=record_id, started_at=started_at, duration_s=45,
                  training_context={"kind": "free", "cycle_id": "normal-practice"})
    record.pop("sensitivity_context")
    record["settings"]["duration_s"] = 45
    for i in range(401, 901):
        record["samples"].append(dict(record["samples"][-1], t=i * .05))
    report = analyze_record(record)
    for name, value in (metric_values or {}).items():
        report["metrics"][name]["value"] = value
    return {"record": record, "report": report}


class SensitivityTests(unittest.TestCase):
    def test_balanced_matched_schedule_preserves_all_non_sensitivity_settings(self):
        settings = copy.deepcopy(SETTINGS)
        experiment = create_experiment(settings)
        self.assertEqual(settings, SETTINGS)
        self.assertEqual(len(experiment["blocks"]), 12)
        self.assertEqual([candidate["id"] for candidate in experiment["candidates"]], ["lower", "current", "higher"])
        self.assertAlmostEqual(experiment["candidates"][0]["sensitivity_deg_per_count"], .056)
        first, second = experiment["blocks"][:6], experiment["blocks"][6:]
        self.assertEqual([(b["candidate_id"], b["drill"]) for b in first],
                         [(b["candidate_id"], b["drill"]) for b in reversed(second)])
        for repetition in (0, 1):
            for drill in ("clicking", "tracking"):
                blocks = [b for b in experiment["blocks"] if b["repetition"] == repetition and b["drill"] == drill]
                self.assertEqual(len({b["settings"]["seed"] for b in blocks}), 1)
        for block in experiment["blocks"]:
            self.assertEqual(block["settle_s"], 5)
            self.assertEqual(block["settings"]["duration_s"], 20)
            for key in ("fov", "target_scale", "speed_scale", "dpi"):
                self.assertEqual(block["settings"][key], settings[key])

    def test_clamped_duplicates_keep_current_candidate_and_actual_multiplier(self):
        for value, expected in ((.001, ["current", "higher"]), (.5, ["lower", "current"])):
            experiment = create_experiment(SETTINGS | {"sensitivity_deg_per_count": value})
            self.assertEqual([c["id"] for c in experiment["candidates"]], expected)
            self.assertEqual(len(experiment["blocks"]), 8)
            self.assertEqual(next(c for c in experiment["candidates"] if c["id"] == "current")["multiplier"], 1)
        for value in (True, 0, .7, float("nan")):
            with self.assertRaises(ValueError):
                create_experiment(SETTINGS | {"sensitivity_deg_per_count": value})

    def test_rejects_wrong_sequence_settings_context_and_incomplete_rounds(self):
        experiment = create_experiment(SETTINGS)
        record = measured_round(experiment, 0)
        for mutate in (lambda r: r["settings"].update(sensitivity_deg_per_count=.123),
                       lambda r: r["settings"].update(seed=-100),
                       lambda r: r["sensitivity_context"].update(experiment_id="other"),
                       lambda r: r["training_context"].update(kind="practice"),
                       lambda r: r.update(completed=False),
                       lambda r: r.update(duration_s=20.9),
                       lambda r: r["sensitivity_context"].update(candidate_id="wrong")):
            broken = copy.deepcopy(record)
            mutate(broken)
            with self.subTest(broken=broken["sensitivity_context"]):
                with self.assertRaises(ValueError):
                    add_result(experiment, broken, analyze_record(record))
        skipped = measured_round(experiment, 1)
        with self.assertRaises(ValueError):
            add_result(experiment, skipped, analyze_record(skipped))
        self.assertEqual(experiment["record_ids"], [])

    def test_assignment_retry_is_idempotent_but_record_cannot_fill_another_block(self):
        experiment = create_experiment(SETTINGS)
        record = measured_round(experiment, 0)
        report = analyze_record(record)
        updated = add_result(experiment, record, report)
        self.assertEqual(add_result(updated, record, report), updated)
        later = measured_round(updated, 1)
        later["id"] = record["id"]
        with self.assertRaises(ValueError):
            add_result(updated, later, analyze_record(later))

    def test_incomplete_experiment_or_missing_report_abstains(self):
        experiment = create_experiment(SETTINGS)
        analysis = analyze_experiment(experiment, [])
        self.assertEqual(analysis["status"], "incomplete")
        self.assertEqual(analysis["allowed_candidate_ids"], ["current"])
        experiment, reports = complete_experiment()
        self.assertEqual(analyze_experiment(experiment, reports[:-1])["status"], "incomplete")

    def test_consistent_balanced_gain_is_provisional_and_evidence_is_attributable(self):
        better = {"accuracy_pct": 97, "acquisition_ms": 500, "hits": 36,
                  "tracking_error_deg": 1.5, "time_on_target_pct": 70}
        experiment, reports = complete_experiment(lambda c, r, d: better if c == "lower" else {})
        before = copy.deepcopy(experiment)
        analysis = analyze_experiment(experiment, reports)
        self.assertEqual(analysis["status"], "provisional")
        self.assertEqual(analysis["recommended_candidate_id"], "lower")
        self.assertEqual(analysis["confidence"], "medium")
        self.assertEqual(set(analysis["allowed_candidate_ids"]), {"lower", "current", "higher"})
        self.assertEqual(analysis["strong_candidate_ids"], ["lower"])
        self.assertIn("not a proven optimum", analysis["explanation"])
        evidence_ids = [e["id"] for e in analysis["evidence"]]
        self.assertEqual(len(evidence_ids), len(set(evidence_ids)))
        self.assertTrue(all(identifier.startswith(experiment["id"] + ":") for identifier in evidence_ids))
        self.assertEqual(experiment, before)

    def test_accuracy_alone_does_not_win_when_clicking_is_slower(self):
        experiment, reports = complete_experiment(lambda c, r, d:
                    {"accuracy_pct": 100, "acquisition_ms": 800, "hits": 22} if c == "lower" else {})
        analysis = analyze_experiment(experiment, reports)
        self.assertEqual(analysis["recommended_candidate_id"], "current")
        self.assertEqual(analysis["status"], "inconclusive")

    def test_fast_inaccurate_shooting_is_not_recommended(self):
        experiment, reports = complete_experiment(lambda c, r, d:
                    {"accuracy_pct": 85, "acquisition_ms": 300, "hits": 45} if c == "higher" else {})
        analysis = analyze_experiment(experiment, reports)
        self.assertEqual(analysis["recommended_candidate_id"], "current")
        higher = next(c for c in analysis["candidates"] if c["id"] == "higher")
        self.assertIn("accuracy_dropped", higher["regression_flags"])

    def test_conflicting_repeats_do_not_support_a_change(self):
        experiment, reports = complete_experiment(lambda c, r, d:
                    ({"acquisition_ms": 450, "hits": 40} if r == 0 else
                     {"acquisition_ms": 750, "hits": 24}) if c == "lower" else {})
        analysis = analyze_experiment(experiment, reports)
        self.assertEqual(analysis["recommended_candidate_id"], "current")
        lower = next(c for c in analysis["candidates"] if c["id"] == "lower")
        self.assertFalse(lower["repeat_consistency"]["direction_agrees"])

    def test_low_engagement_cannot_recommend_even_with_perfect_tracking_metrics(self):
        experiment, reports = complete_experiment(lambda c, r, d:
                    {"tracking_error_deg": 0, "time_on_target_pct": 100} if c == "higher" else {})
        report = next(report for report in reports if report["drill"] == "tracking")
        next(e for e in report["evidence"] if e["id"].endswith(":aim-summary"))["data"]["observed_s"] = 2
        analysis = analyze_experiment(experiment, reports)
        self.assertEqual(analysis["status"], "inconclusive")
        self.assertEqual(analysis["allowed_candidate_ids"], ["current"])
        self.assertIn("only 2.0 s of usable engaged tracking", analysis["explanation"])
        self.assertNotIn("holding fire", analysis["explanation"])
        self.assertNotIn("repeat the screening", analysis["explanation"])

    def test_mixed_repeats_explain_current_choice_with_speed_and_accuracy_tradeoff(self):
        def performance(candidate, repetition, drill):
            if candidate == "current":
                return {"acquisition_ms": [730, 600][repetition], "accuracy_pct": 97}
            if candidate == "lower":
                return {"acquisition_ms": [670, 600][repetition], "accuracy_pct": [97, 92][repetition]}
            return {"acquisition_ms": [750, 620][repetition], "accuracy_pct": 93}

        experiment, reports = complete_experiment(performance)
        analysis = analyze_experiment(experiment, reports)
        self.assertEqual(analysis["status"], "inconclusive")
        self.assertEqual(set(analysis["allowed_candidate_ids"]), {"lower", "current", "higher"})
        self.assertEqual(analysis["strong_candidate_ids"], [])
        lower = next(c for c in analysis["candidates"] if c["id"] == "lower")
        self.assertIn("30 ms faster acquisition", lower["comparison_summary"])
        self.assertIn("2.5 percentage points lower accuracy", lower["comparison_summary"])
        self.assertIn("Repeat 1: 60 ms faster", lower["comparison_summary"])
        self.assertIn("Repeat 2: similar acquisition time (600 vs 600 ms); accuracy 92.0% vs 97.0%", lower["comparison_summary"])
        self.assertIn("92.0% vs 97.0%", analysis["explanation"])
        self.assertIn("Higher averaged 20 ms slower acquisition", analysis["explanation"])
        self.assertNotIn("try another", analysis["explanation"])

    def test_quality_fallback_identifies_recording_problem_without_guessing_controls(self):
        experiment, reports = complete_experiment()
        report = reports[0]
        report["quality"].update(flags=["low_sample_cadence"], usable_for_coaching=False, sample_hz=8)
        analysis = analyze_experiment(experiment, reports)
        self.assertEqual(analysis["status"], "inconclusive")
        self.assertEqual(analysis["allowed_candidate_ids"], ["current"])
        self.assertIn("recording cadence of 8.0 samples/s", analysis["explanation"])
        self.assertNotIn("holding fire", analysis["explanation"])
        self.assertNotIn("clicking each target", analysis["explanation"])
        self.assertNotIn("repeat the screening", analysis["explanation"])

    def test_tracking_tradeoff_is_named_in_repeat_explanation(self):
        experiment, reports = complete_experiment(lambda c, r, d:
                    {"tracking_error_deg": 3, "time_on_target_pct": 40} if c == "lower" and r == 1 else {})
        analysis = analyze_experiment(experiment, reports)
        lower = next(c for c in analysis["candidates"] if c["id"] == "lower")
        self.assertEqual(analysis["recommended_candidate_id"], "current")
        self.assertIn("tracking error 3.00° vs 2.00°", lower["comparison_summary"])
        self.assertIn("time on target 40.0% vs 60.0%", lower["comparison_summary"])
        self.assertIn("tracking error 3.00° vs 2.00°", analysis["explanation"])

    def test_indistinguishable_alternatives_default_to_current(self):
        experiment, reports = complete_experiment(lambda c, r, d:
                    {"acquisition_ms": 500, "hits": 36} if c != "current" else {})
        analysis = analyze_experiment(experiment, reports)
        self.assertEqual(analysis["status"], "inconclusive")
        self.assertEqual(analysis["recommended_candidate_id"], "current")
        self.assertIn("cannot distinguish", analysis["explanation"])

    def test_foreign_duplicate_or_mismatched_reports_are_rejected(self):
        experiment, reports = complete_experiment()
        with self.assertRaises(ValueError):
            analyze_experiment(experiment, reports + [reports[0]])
        foreign = copy.deepcopy(reports)
        foreign[0]["record_id"] = "other"
        with self.assertRaises(ValueError):
            analyze_experiment(experiment, foreign)
        mismatched = copy.deepcopy(reports)
        mismatched[0]["benchmark_key"] = "different"
        with self.assertRaises(ValueError):
            analyze_experiment(experiment, mismatched)

    def test_one_extra_miss_does_not_prohibit_an_ai_sensitivity_trial(self):
        def performance(candidate, repetition, drill):
            return {"accuracy_pct": 100 * (29 if candidate == "lower" else 30) / 30,
                    "hits": 29 if candidate == "lower" else 30,
                    "shots": 30, "acquisition_ms": 550 if candidate == "lower" else 600}

        experiment, reports = complete_experiment(performance)
        analysis = analyze_experiment(experiment, reports)
        lower = next(candidate for candidate in analysis["candidates"] if candidate["id"] == "lower")
        self.assertTrue(lower["eligible"])
        self.assertIn("lower", analysis["allowed_candidate_ids"])
        self.assertIn("accuracy_dropped", lower["tradeoff_flags"])
        self.assertFalse(lower["strong_evidence"])
        self.assertEqual(analysis["strong_candidate_ids"], [])
        self.assertEqual(analysis["confidence"], "low")
        self.assertEqual(analysis["recommended_candidate_id"], "current")

    def test_matched_follow_up_adds_observational_evidence_without_rewriting_screen(self):
        experiment, reports = complete_experiment()
        original = analyze_experiment(experiment, reports)
        cutoff = original["screen_completed_at"]
        before = normal_round(experiment, "current", "normal-before", cutoff + 100,
                              {"accuracy_pct": 94, "acquisition_ms": 690, "overshoot_pct": 25})
        after = normal_round(experiment, "lower", "normal-after", cutoff + 200,
                             {"accuracy_pct": 97, "acquisition_ms": 627, "overshoot_pct": 9})
        updated = add_follow_up(original, experiment, [after, before])
        self.assertEqual(updated["candidates"], original["candidates"])
        self.assertEqual(updated["recommended_candidate_id"], original["recommended_candidate_id"])
        self.assertEqual(updated["confidence"], original["confidence"])
        self.assertNotIn("follow_up", original)
        follow_up = updated["follow_up"]
        self.assertEqual(follow_up["status"], "available")
        self.assertEqual(follow_up["record_ids"], ["normal-before", "normal-after"])
        pair = follow_up["comparisons"][0]
        self.assertTrue(pair["settings_matched"])
        self.assertEqual(pair["metrics"]["accuracy_pct"]["delta"], 3)
        self.assertEqual(pair["metrics"]["acquisition_ms"]["delta"], -63)
        self.assertEqual(pair["metrics"]["overshoot_pct"]["delta"], -16)
        self.assertIn("One observational pair", pair["limitation"])
        self.assertIn("does not establish", pair["limitation"])
        self.assertIn(pair["evidence_id"], {item["id"] for item in updated["evidence"]})
        self.assertEqual(add_follow_up(updated, experiment, [after, before]), updated)

    def test_follow_up_rejects_untested_or_unmatched_settings_and_poor_quality(self):
        experiment, reports = complete_experiment()
        cutoff = experiment["completed_at"]
        before = normal_round(experiment, "current", "normal-before", cutoff + 100)
        after = normal_round(experiment, "lower", "normal-after", cutoff + 200)
        for field, value in (("fov", 90), ("target_scale", 1.2), ("speed_scale", 1.2),
                             ("duration_s", 30), ("sensitivity_deg_per_count", .063)):
            changed = copy.deepcopy(after)
            changed["record"]["settings"][field] = value
            changed["report"] = analyze_record(changed["record"])
            with self.subTest(field=field):
                self.assertEqual(analyze_follow_up(experiment, [before, changed])["record_ids"], [])
        for mutate in (lambda item: item["record"].update(completed=False),
                       lambda item: item["record"].update(started_at=cutoff - 1),
                       lambda item: item["record"]["training_context"].update(kind="sensitivity")):
            changed = copy.deepcopy(after)
            mutate(changed)
            changed["report"] = analyze_record(changed["record"])
            self.assertEqual(analyze_follow_up(experiment, [before, changed])["status"], "none")
        changed = copy.deepcopy(after)
        changed["report"]["quality"]["usable_for_coaching"] = False
        self.assertEqual(analyze_follow_up(experiment, [before, changed])["status"], "none")

    def test_follow_up_uses_latest_matched_pair_and_handles_legacy_cutoff(self):
        experiment, reports = complete_experiment()
        analysis = analyze_experiment(experiment, reports)
        cutoff = analysis["screen_completed_at"]
        rounds = [normal_round(experiment, "current", "before-old", cutoff + 100),
                  normal_round(experiment, "current", "before-latest", cutoff + 200),
                  normal_round(experiment, "lower", "after-old", cutoff + 300),
                  normal_round(experiment, "lower", "after-latest", cutoff + 400)]
        # The completed old experiment need not have newly introduced metadata;
        # fresh screen analysis knows the actual recorded completion time.
        experiment.pop("completed_at")
        self.assertEqual(analyze_follow_up(experiment, rounds)["status"], "none")
        follow_up = add_follow_up(analysis, experiment, rounds)["follow_up"]
        self.assertEqual(follow_up["record_ids"], ["before-latest", "after-latest"])
        self.assertEqual(len(follow_up["comparisons"]), 1)

    def test_later_prescribed_practice_does_not_hide_the_free_sensitivity_comparison(self):
        experiment, reports = complete_experiment()
        cutoff = experiment["completed_at"]
        before = normal_round(experiment, "current", "normal-before", cutoff + 100)
        after = normal_round(experiment, "lower", "normal-after", cutoff + 200)
        practice = normal_round(experiment, "lower", "practice-after", cutoff + 300)
        practice["record"]["training_context"]["kind"] = "practice"
        practice["report"] = analyze_record(practice["record"])
        follow_up = analyze_follow_up(experiment, [practice, after, before])
        self.assertEqual(len(follow_up["comparisons"]), 2)
        self.assertEqual({pair["after_training_kind"] for pair in follow_up["comparisons"]}, {"free", "practice"})
        self.assertEqual(set(follow_up["record_ids"]), {"normal-before", "normal-after", "practice-after"})
        self.assertIn("not independent repeats", follow_up["summary"])


if __name__ == "__main__":
    unittest.main()
