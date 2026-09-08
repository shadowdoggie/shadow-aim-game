import copy
import math
import json
import unittest

from companion.metrics import analyze_record, angular_error, benchmark_key


def session(record_id="test-round", drill="clicking", duration=1.0):
    return {"schema_version": 1, "id": record_id, "drill": drill, "started_at": 1000.0,
            "duration_s": duration, "completed": True,
            "settings": {"fov": 103.0, "sensitivity_deg_per_count": .025,
                         "target_scale": 1.0, "speed_scale": 1.0, "seed": 12,
                         "duration_s": duration, "fov_axis": "horizontal", "drill_version": 1},
            "samples": [], "shots": [], "events": []}


def sample(t, yaw=0.0, target_yaw=0.0, target_id="target-1", on_target=True, engaged=True, frame_ms=4.2):
    return {"t": t, "yaw": yaw, "pitch": 0.0, "target_id": target_id, "target_yaw": target_yaw,
            "target_pitch": 0.0, "target_radius": 1.0, "on_target": on_target,
            "engaged": engaged, "frame_ms": frame_ms}


def event(t, kind, target_id="target-1", yaw=10.0):
    return {"t": t, "type": kind, "target_id": target_id, "yaw": yaw, "pitch": 0.0, "radius": 1.0}


class MeasurementTests(unittest.TestCase):
    def test_godot_integral_float_round_trip_acceptance_and_benchmark(self):
        original = session()
        original["settings"].update(seed=734, duration_s=45, fov=103)
        reencoded = json.loads(json.dumps(original), parse_int=float)
        self.assertIs(type(reencoded["schema_version"]), float)
        self.assertEqual(analyze_record(reencoded)["benchmark_key"], analyze_record(original)["benchmark_key"])
        self.assertEqual(benchmark_key(reencoded), benchmark_key(original))
        self.assertIs(type(reencoded["settings"]["seed"]), float)

    def test_numeric_schema_and_seed_reject_non_integral_nonfinite_and_booleans(self):
        for field, values in (("schema_version", (1.2, True, False, float("nan"), float("inf"))),
                              ("seed", (734.5, True, False, float("nan"), float("inf")))):
            for value in values:
                with self.subTest(field=field, value=value):
                    record = session()
                    if field == "seed":
                        record["settings"][field] = value
                    else:
                        record[field] = value
                    with self.assertRaises(ValueError):
                        analyze_record(record)

    def test_overshoot_and_correction_have_known_geometric_meaning(self):
        record = session(duration=.5)
        aims = [0, 2, 4, 6, 8, 10, 12, 12, 11, 10.5, 10]
        record["samples"] = [sample(i * .05, yaw, 10, on_target=abs(yaw-10)<=1) for i, yaw in enumerate(aims)]
        record["shots"] = [{"t": .3, "hit": False, "target_id": "target-1", "error_deg": 2},
                           {"t": .5, "hit": True, "target_id": "target-1", "error_deg": 0}]
        record["events"] = [event(0, "spawn"), event(.5, "hit")]
        report = analyze_record(record)
        self.assertEqual(report["metrics"]["accuracy_pct"]["value"], 50)
        self.assertEqual(report["metrics"]["acquisition_ms"]["value"], 500)
        self.assertEqual(report["metrics"]["overshoot_pct"]["value"], 100)
        self.assertEqual(report["metrics"]["correction_ms"]["value"], 200)
        self.assertIn("short_run", report["quality"]["flags"])

    def test_clean_approach_is_not_scored_as_overshoot(self):
        record = session(duration=.5)
        record["samples"] = [sample(i * .05, i, 10, on_target=i >= 9) for i in range(11)]
        record["shots"] = [{"t": .5, "hit": True, "target_id": "target-1", "error_deg": 0}]
        record["events"] = [event(0, "spawn"), event(.5, "hit")]
        report = analyze_record(record)
        self.assertEqual(report["metrics"]["overshoot_pct"]["value"], 0)
        self.assertIsNone(report["metrics"]["correction_ms"]["value"])

    def test_moving_target_cannot_produce_static_overshoot_claim(self):
        record = session(drill="tracking", duration=.5)
        record["samples"] = [sample(i * .05, i * 2, 10+i) for i in range(11)]
        record["shots"] = [{"t": .5, "hit": True, "target_id": "target-1", "error_deg": 0}]
        record["events"] = [event(0, "spawn"), event(.5, "hit")]
        report = analyze_record(record)
        self.assertIsNone(report["metrics"]["overshoot_pct"]["value"])

    def test_yaw_wrap_and_time_weighting_do_not_inflate_tracking_error(self):
        record = session(drill="tracking")
        # Half a second on target, then half a second two degrees across the seam.
        record["samples"] = [sample(i * .05, 179 if i < 10 else -179, 179,
                                    on_target=i < 10) for i in range(21)]
        report = analyze_record(record)
        self.assertAlmostEqual(report["metrics"]["tracking_error_deg"]["value"], 1, places=4)
        self.assertAlmostEqual(report["metrics"]["time_on_target_pct"]["value"], 50, places=4)
        self.assertAlmostEqual(angular_error(-179, 0, 179, 0), 2, places=6)
        # Near a pole, yaw differences are not distances in viewing space.
        self.assertLess(angular_error(0, 80, 10, 80), 2)

    def test_sample_gaps_are_not_filled_with_invented_good_tracking(self):
        record = session(drill="tracking")
        record["samples"] = [sample(0), sample(.05), sample(.95, yaw=10, on_target=False),
                             sample(1, yaw=10, on_target=False)]
        report = analyze_record(record)
        self.assertEqual(report["metrics"]["time_on_target_pct"]["value"], 50)
        self.assertIn("sample_gaps", report["quality"]["flags"])
        self.assertFalse(report["quality"]["usable_for_coaching"])
        aggregate = next(e for e in report["evidence"] if e["id"] == "test-round:aim-summary")
        self.assertAlmostEqual(aggregate["data"]["observed_s"], .1)

    def test_only_engaged_tracking_counts_and_edge_events_are_not_frames(self):
        record = session(drill="tracking", duration=.15)
        record["samples"] = [sample(0, engaged=False, frame_ms=0), sample(.05, engaged=False, frame_ms=0),
                             sample(.1, yaw=2, on_target=False, frame_ms=5),
                             sample(.15, yaw=2, on_target=False, frame_ms=5)]
        report = analyze_record(record)
        self.assertEqual(report["metrics"]["tracking_error_deg"]["value"], 2)
        self.assertEqual(report["metrics"]["time_on_target_pct"]["value"], 0)
        self.assertEqual(report["metrics"]["frame_p95_ms"]["value"], 5)

    def test_short_runs_and_few_clicks_are_not_confident_coaching_evidence(self):
        short = session(drill="tracking", duration=5)
        short["samples"] = [sample(i * .02) for i in range(251)]
        short_report = analyze_record(short)
        self.assertTrue(short_report["valid"])
        self.assertIn("short_run", short_report["quality"]["flags"])
        self.assertFalse(short_report["quality"]["usable_for_coaching"])
        long = session(duration=30)
        long["samples"] = [sample(i * .02) for i in range(1501)]
        long["shots"] = [{"t": i + 1, "hit": True, "target_id": "target-1", "error_deg": 0}
                         for i in range(9)]
        few_report = analyze_record(long)
        self.assertIn("low_attempts", few_report["quality"]["flags"])
        self.assertFalse(few_report["quality"]["usable_for_coaching"])
        long["shots"].append({"t": 10, "hit": True, "target_id": "target-1", "error_deg": 0})
        self.assertTrue(analyze_record(long)["quality"]["usable_for_coaching"])

    def test_long_tracking_round_with_tiny_held_sample_is_insufficient(self):
        record = session(drill="tracking", duration=45)
        record["samples"] = [sample(i * .02, engaged=i < 101) for i in range(2251)]
        report = analyze_record(record)
        self.assertTrue(report["valid"])
        self.assertEqual(report["metrics"]["time_on_target_pct"]["value"], 100)
        self.assertEqual(report["quality"]["flags"], ["low_tracking_engagement"])
        self.assertFalse(report["quality"]["usable_for_coaching"])
        record["samples"] = [sample(i * .02, engaged=i <= 750) for i in range(2251)]
        self.assertTrue(analyze_record(record)["quality"]["usable_for_coaching"])

    def test_training_links_do_not_change_benchmark_or_mutate_record(self):
        record = session()
        expected_key = benchmark_key(record)
        self.assertNotIn("training_context", analyze_record(record))
        record["training_context"] = {"kind": "retest", "cycle_id": "cycle-1", "baseline_record_id": "before",
                                      "source_coaching_record_id": "advice", "block_index": 2.0}
        snapshot = copy.deepcopy(record)
        report = analyze_record(record)
        self.assertEqual(report["benchmark_key"], expected_key)
        self.assertEqual(report["training_context"], record["training_context"])
        self.assertIs(type(report["training_context"]["block_index"]), int)
        report["training_context"]["kind"] = "practice"
        self.assertEqual(record, snapshot)
        for context in ({}, {"kind": "free", "cycle_id": "", "baseline_record_id": ""}):
            record["training_context"] = context
            self.assertEqual(analyze_record(record)["training_context"], context)

    def test_malformed_training_links_are_rejected(self):
        for context in (None, [], {"kind": "unknown"}, {"kind": "retest", "cycle_id": "../x"},
                        {"kind": "practice", "source_coaching_record_id": None},
                        {"kind": "practice", "baseline_record_id": 1},
                        {"kind": "baseline", "block_index": True},
                        {"kind": "baseline", "block_index": 1.5},
                        {"kind": "baseline", "block_index": -1},
                        {"kind": "baseline", "unexpected": 1}):
            with self.subTest(context=context):
                record = session()
                record["training_context"] = context
                with self.assertRaises(ValueError):
                    analyze_record(record)

    def test_benchmark_matches_fresh_seed_but_not_changed_setup(self):
        first = session()
        second = copy.deepcopy(first)
        second["settings"]["seed"] += 1
        second["duration_s"] += .002  # Render-frame finish jitter is irrelevant.
        self.assertEqual(benchmark_key(first), benchmark_key(second))
        for field, value in (("fov", 90), ("sensitivity_deg_per_count", .03), ("target_scale", 2),
                             ("speed_scale", 2), ("duration_s", 2), ("drill_version", 2)):
            changed = copy.deepcopy(first)
            changed["settings"][field] = value
            self.assertNotEqual(benchmark_key(first), benchmark_key(changed), field)

    def test_evidence_is_attributable_across_session_context(self):
        first = session("baseline")
        first["samples"] = [sample(0), sample(.05)]
        second = copy.deepcopy(first)
        second["id"] = "retest"
        reports = [analyze_record(record) for record in (first, second)]
        ids = [{item["id"] for item in report["evidence"]} for report in reports]
        self.assertFalse(ids[0] & ids[1])
        for report, available in zip(reports, ids):
            for metric in report["metrics"].values():
                self.assertTrue(set(metric["evidence_ids"]) <= available)

    def test_nonfinite_unordered_and_path_ids_rejected(self):
        base = session()
        base["samples"] = [sample(0), sample(.05)]
        for mutate in (lambda r: r.update(id="../escape"),
                       lambda r: r["samples"][0].update(yaw=math.nan),
                       lambda r: r["samples"].reverse(),
                       lambda r: r["samples"][0].update(t=2),
                       lambda r: r["shots"].append({"t": .1, "hit": True, "target_id": "t", "error_deg": math.inf})):
            bad = copy.deepcopy(base)
            mutate(bad)
            with self.assertRaises(ValueError):
                analyze_record(bad)


if __name__ == "__main__":
    unittest.main()
