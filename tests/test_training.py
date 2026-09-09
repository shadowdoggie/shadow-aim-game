import tempfile
import unittest
from unittest.mock import patch

from companion.storage import Storage
from companion.training import BASELINE_MODES, contextual_history, next_training_action
from tests.test_metrics import event, sample, session


class TrainingProgressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = Storage(self.temp.name)
        self.timestamp = 1000

    def save(self, identifier, mode="clicking", kind="baseline", cycle="baseline-cycle",
             baseline="", source="", changes=None, usable=True):
        self.timestamp += 100
        drill = "tracking" if mode == "reactive_tracking" else mode
        raw = session(identifier, drill, 45)
        raw["started_at"] = self.timestamp
        raw["training_context"] = {"kind": kind, "cycle_id": cycle}
        if baseline:
            raw["training_context"]["baseline_record_id"] = baseline
        if source:
            raw["training_context"]["source_coaching_record_id"] = source
        if mode == "reactive_tracking":
            raw["settings"]["tracking_motion"] = "reactive"
        raw["settings"].update(changes or {})
        raw["duration_s"] = raw["settings"]["duration_s"]
        raw["samples"] = [sample(index / 20, engaged=usable) for index in range(int(raw["duration_s"] * 20) + 1)]
        raw["events"] = [event(0, "spawn")]
        if drill != "tracking" and usable:
            raw["shots"] = [{"t": index + .2, "target_id": "target-1", "hit": True, "error_deg": 0}
                            for index in range(10)]
        self.storage.save_record(raw)
        return raw

    def baseline(self):
        for mode in BASELINE_MODES:
            self.save("base-" + mode, mode)

    def coach(self, source="base-switching", drill="clicking", evidence="base-clicking"):
        plan = {"drill": drill, "cue": "Finish the adjustment before clicking.",
                "parameters": {"duration_s": 30, "target_scale": 1.2, "speed_scale": 1},
                "evidence_ids": [evidence + ":aim-summary"], "decision": "progress"}
        self.storage.save_coaching(source, plan)
        return plan

    def practice(self, identifier, cycle="practice-cycle", changes=None, usable=True):
        return self.save(identifier, kind="practice", cycle=cycle, baseline="base-clicking", source="base-switching",
                         changes={"duration_s": 30, "target_scale": 1.2} | (changes or {}), usable=usable)

    def test_fresh_and_legacy_baseline_resume_only_missing_modes(self):
        self.assertEqual(next_training_action(self.storage)["missing_modes"], list(BASELINE_MODES))
        for mode in ("clicking", "tracking", "switching"):
            self.save("base-" + mode, mode)
        self.save("one-off", cycle="different-cycle")
        result = next_training_action(self.storage)
        self.assertEqual(result["action"], "baseline")
        self.assertEqual(result["missing_modes"], ["reactive_tracking"])
        self.assertEqual(result["cycle_id"], "baseline-cycle")

    def test_different_cycles_or_profile_settings_do_not_make_complete_baseline(self):
        self.save("click", cycle="a")
        self.save("smooth", "tracking", cycle="a")
        self.save("reactive", "reactive_tracking", cycle="b")
        self.save("switch", "switching", cycle="b")
        self.assertFalse(next_training_action(self.storage)["baseline_complete"])
        self.baseline()
        result = next_training_action(self.storage, {"sensitivity_deg_per_count": .05, "fov": 103})
        self.assertEqual(result["missing_modes"], list(BASELINE_MODES))

    def test_low_engagement_tracking_cannot_complete_baseline(self):
        for mode in BASELINE_MODES:
            self.save("base-" + mode, mode, usable=mode != "reactive_tracking")
        self.assertEqual(next_training_action(self.storage)["missing_modes"], ["reactive_tracking"])

    def test_same_cycle_with_changed_profile_is_not_a_complete_baseline(self):
        for mode in BASELINE_MODES:
            self.save("base-" + mode, mode, changes={"fov": 90} if mode == "switching" else None)
        self.assertFalse(next_training_action(self.storage)["baseline_complete"])

    def test_complete_baseline_reviews_once_with_all_four_modes_across_restart(self):
        for mode in BASELINE_MODES:
            self.timestamp += 3600  # A interrupted baseline can continue another day.
            self.save("base-" + mode, mode)
        self.storage = Storage(self.temp.name)
        result = next_training_action(self.storage)
        self.assertEqual(result["action"], "review")
        self.assertEqual(result["record_id"], "base-switching")
        self.assertNotIn("samples", result["record"])
        history = contextual_history(self.storage, self.storage.get_report(result["record_id"]))
        self.assertEqual({item["record_id"] for item in history},
                         {"base-clicking", "base-tracking", "base-reactive_tracking"})

    def test_approved_cross_drill_practice_preserves_cited_reactive_mode(self):
        self.baseline()
        plan = self.coach(drill="tracking", evidence="base-reactive_tracking")
        result = next_training_action(self.storage)
        self.assertEqual(result["action"], "coached_practice")
        self.assertEqual(result["mode"], "reactive_tracking")
        self.assertEqual(result["settings"]["tracking_motion"], "reactive")
        self.assertEqual(result["baseline_record"]["id"], "base-reactive_tracking")
        self.assertEqual(result["recommendation"], plan)
        self.assertEqual(result["remaining_rounds"], 2)
        self.assertEqual(result["cycle_id"], "")

    def test_ambiguous_tracking_advice_is_reviewed_instead_of_assuming_smooth(self):
        self.baseline()
        self.coach(drill="tracking", evidence="base-switching")
        result = next_training_action(self.storage)
        self.assertEqual(result["action"], "review")
        self.assertIsNone(result["settings"])

    def test_practice_count_uses_current_cycle_links_settings_and_quality(self):
        self.baseline()
        self.coach()
        self.practice("old-practice", cycle="older-cycle")
        self.practice("wrong-settings", changes={"target_scale": 1.5})
        self.practice("low-attempts", usable=False)
        self.practice("practice-1")
        self.storage = Storage(self.temp.name)
        result = next_training_action(self.storage)
        self.assertEqual(result["action"], "continue_practice")
        self.assertEqual(result["remaining_rounds"], 1)
        self.assertEqual(result["cycle_id"], "practice-cycle")
        self.assertEqual(result["source_coaching_record_id"], "base-switching")

    def test_two_practices_retest_exact_original_settings_then_review_new_results(self):
        self.baseline()
        self.coach()
        self.practice("practice-1")
        self.practice("practice-2")
        result = next_training_action(self.storage)
        self.assertEqual(result["action"], "retest")
        baseline = self.storage.get_session("base-clicking")
        self.assertEqual(result["settings"], {key: value for key, value in baseline["settings"].items() if key != "seed"})
        self.save("retest", kind="retest", cycle="practice-cycle", baseline="base-clicking", source="base-switching")
        result = next_training_action(self.storage)
        self.assertEqual((result["action"], result["record_id"]), ("review", "retest"))
        self.coach(source="retest", evidence="retest")
        result = next_training_action(self.storage)
        self.assertEqual(result["action"], "coached_practice")
        self.assertEqual(result["baseline_record"]["id"], "retest")
        self.assertEqual(result["source_coaching_record_id"], "retest")

    def test_changed_practice_settings_require_review_not_old_plan_or_retest(self):
        self.baseline()
        self.coach()
        self.practice("practice", changes={"target_scale": 1.6})
        self.assertEqual(next_training_action(self.storage)["action"], "review")
        self.coach(source="practice", evidence="practice")
        result = next_training_action(self.storage)
        self.assertEqual(result["action"], "coached_practice")
        self.assertEqual(result["source_coaching_record_id"], "practice")
        self.assertEqual(result["baseline_record"]["id"], "base-clicking")

    def test_sensitivity_rounds_do_not_replace_ordinary_next_action(self):
        self.baseline()
        self.coach()
        self.save("sensitivity", kind="sensitivity", cycle="sensitivity-screen")
        result = next_training_action(self.storage)
        self.assertEqual(result["action"], "coached_practice")
        self.assertEqual(result["record_id"], "base-switching")

    def test_over_one_hundred_free_rounds_do_not_erase_initial_baseline(self):
        self.baseline()
        for index in range(101):
            self.save("free-" + str(index), kind="free", cycle="free-" + str(index))
        reopened = Storage(self.temp.name)
        with patch.object(reopened, "get_session", wraps=reopened.get_session) as reads:
            result = next_training_action(reopened)
        self.assertEqual(reads.call_count, 5)  # Four baseline traces and the newest ordinary round.
        self.assertTrue(result["baseline_complete"])
        self.assertEqual((result["action"], result["record_id"]), ("review", "free-100"))


if __name__ == "__main__":
    unittest.main()
