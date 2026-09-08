import copy
import tempfile
import unittest

from companion.metrics import analyze_record, benchmark_key
from companion.storage import Storage
from tests.test_metrics import sample, session


class ReactiveTrackingMetricsTests(unittest.TestCase):
    def test_motion_label_does_not_change_legacy_settings_or_measurements(self):
        smooth = session(drill="tracking")
        smooth["samples"] = [sample(i * .05) for i in range(21)]
        original = copy.deepcopy(smooth)
        reactive = copy.deepcopy(smooth)
        reactive["settings"]["tracking_motion"] = "reactive"
        smooth_report = analyze_record(smooth)
        reactive_report = analyze_record(reactive)
        self.assertEqual(smooth, original)
        self.assertNotIn("tracking_motion", smooth["settings"])
        self.assertEqual(smooth_report["tracking_motion"], "smooth")
        self.assertEqual(reactive_report["tracking_motion"], "reactive")
        self.assertEqual(smooth_report["metrics"], reactive_report["metrics"])
        self.assertNotIn("tracking_motion", analyze_record(session(drill="clicking")))

    def test_reactive_history_is_separate_but_equivalent_seeds_match(self):
        smooth = session("smooth-round", drill="tracking")
        smooth["samples"] = [sample(i * .05) for i in range(21)]
        reactive = copy.deepcopy(smooth)
        reactive.update(id="reactive-first", started_at=1001)
        reactive["settings"]["tracking_motion"] = "reactive"
        retest = copy.deepcopy(reactive)
        retest.update(id="reactive-retest", started_at=1002)
        retest["settings"]["seed"] = 999
        self.assertNotEqual(benchmark_key(smooth), benchmark_key(reactive))
        self.assertEqual(benchmark_key(reactive), benchmark_key(retest))
        with tempfile.TemporaryDirectory() as temp:
            storage = Storage(temp)
            storage.save_record(smooth)
            storage.save_record(reactive)
            report = storage.save_record(retest)
            history = storage.matching_history(report)
            self.assertEqual([item["record_id"] for item in history], ["reactive-first"])
            self.assertEqual(history[0]["tracking_motion"], "reactive")


if __name__ == "__main__":
    unittest.main()
