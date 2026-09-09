import copy
import math
import unittest

from companion.metrics import analyze_record, angular_error, shot_position
from tests.test_metrics import sample, session


def located_shot(t, yaw=0.0, pitch=0.0, target_yaw=0.0, target_pitch=0.0, radius=1.0, hit=True):
    return {"t": t, "hit": hit, "target_id": "target-1", "aim_yaw": yaw, "aim_pitch": pitch,
        "target_yaw": target_yaw, "target_pitch": target_pitch, "target_radius": radius,
        "error_deg": angular_error(yaw, pitch, target_yaw, target_pitch)}


class ShotPlacementTests(unittest.TestCase):
    def test_center_sides_and_vertical_offsets_use_target_radius(self):
        for yaw, pitch, expected_x, expected_y in ((0, 0, 0, 0), (.5, 0, .5, 0),
                (-.8, 0, -.8, 0), (0, .9, 0, .9), (0, -.4, 0, -.4)):
            with self.subTest(yaw=yaw, pitch=pitch):
                offset = shot_position(located_shot(0, yaw=yaw, pitch=pitch))
                self.assertAlmostEqual(offset["x_radius"], expected_x, places=6)
                self.assertAlmostEqual(offset["y_radius"], expected_y, places=6)
                self.assertAlmostEqual(offset["radial_radius"], math.hypot(expected_x, expected_y), places=6)
        self.assertAlmostEqual(shot_position(located_shot(0, yaw=1, radius=2))["x_radius"], .5, places=6)

    def test_yaw_seam_does_not_reverse_hit_direction(self):
        offset = shot_position(located_shot(0, yaw=-179.7, target_yaw=179.8))
        self.assertAlmostEqual(offset["x_radius"], .5, places=6)
        self.assertAlmostEqual(offset["y_radius"], 0, places=6)

    def test_high_pitch_uses_exact_local_tangent_axes(self):
        # Construct a great-circle displacement exactly 0.7 degrees to target-right.
        yaw, pitch, angle = map(math.radians, (40, 80, .7))
        center = (math.sin(yaw) * math.cos(pitch), math.sin(pitch), -math.cos(yaw) * math.cos(pitch))
        right = (math.cos(yaw), 0, math.sin(yaw))
        aim = [math.cos(angle) * c + math.sin(angle) * r for c, r in zip(center, right)]
        offset = shot_position(located_shot(0, yaw=math.degrees(math.atan2(aim[0], -aim[2])),
            pitch=math.degrees(math.asin(aim[1])), target_yaw=40, target_pitch=80))
        self.assertAlmostEqual(offset["x_radius"], .7, places=6)
        self.assertAlmostEqual(offset["y_radius"], 0, places=6)

    def test_aggregates_exclude_misses_and_unknown_legacy_hits(self):
        record = session(duration=10)
        record["shots"] = [located_shot(1), located_shot(2, yaw=.4),
            located_shot(3, yaw=-.9), located_shot(4, pitch=.9), located_shot(5, yaw=2, hit=False),
            {"t": 6, "hit": True, "target_id": "target-1", "error_deg": .3}]
        report = analyze_record(record)
        metrics = report["metrics"]
        self.assertEqual(metrics["hit_position_coverage_pct"]["value"], 80)
        self.assertEqual(metrics["center_hit_pct"]["value"], 50)
        self.assertEqual(metrics["edge_hit_pct"]["value"], 50)
        self.assertEqual(metrics["mean_hit_offset_pct_radius"]["value"], 55)
        self.assertEqual(metrics["hit_horizontal_bias_pct_radius"]["value"], -12.5)
        self.assertEqual(metrics["hit_vertical_bias_pct_radius"]["value"], 22.5)
        placement = report["shot_placement"]
        self.assertEqual((placement["located_shots"], placement["located_hits"], placement["unknown_position_hits"]), (5, 4, 1))
        self.assertEqual(placement["zones"], {"center": 2, "middle": 0, "edge": 2})
        self.assertEqual(placement["hit_directions"], {"left": 1, "right": 1, "above": 1, "below": 0})
        self.assertEqual(metrics["center_hit_pct"]["evidence_ids"], ["test-round:shot-placement-summary"])
        evidence = next(e for e in report["evidence"] if e["id"] == placement["evidence_id"])
        self.assertEqual(evidence["data"]["examples"][-1]["hit"], False)

    def test_zone_boundaries_are_explicit_and_not_double_counted(self):
        record = session()
        record["shots"] = [located_shot(.1, yaw=.5), located_shot(.2, yaw=.6), located_shot(.3, yaw=.8)]
        report = analyze_record(record)
        self.assertEqual(report["shot_placement"]["zones"], {"center": 1, "middle": 1, "edge": 1})

    def test_old_shots_remain_unknown_even_with_neighboring_aim_samples(self):
        record = session()
        record["samples"] = [sample(0), sample(.5), sample(1)]
        record["shots"] = [{"t": .5, "hit": True, "target_id": "target-1", "error_deg": .2}]
        snapshot = copy.deepcopy(record)
        report = analyze_record(record)
        self.assertEqual(record, snapshot)
        self.assertFalse(report["shot_placement"]["available"])
        self.assertEqual(report["shot_placement"]["unknown_position_hits"], 1)
        self.assertEqual(report["metrics"]["hit_position_coverage_pct"]["value"], 0)
        for key in ("center_hit_pct", "edge_hit_pct", "mean_hit_offset_pct_radius",
                "hit_horizontal_bias_pct_radius", "hit_vertical_bias_pct_radius"):
            self.assertIsNone(report["metrics"][key]["value"])
            self.assertEqual(report["metrics"][key]["evidence_ids"], [])

    def test_invalid_partial_nonfinite_or_contradictory_positions_are_rejected(self):
        for mutate in (lambda shot: shot.pop("target_pitch"),
                lambda shot: shot.update(aim_yaw=float("nan")),
                lambda shot: shot.update(target_radius=0),
                lambda shot: shot.update(error_deg=30),
                lambda shot: shot.update(aim_yaw=2, error_deg=2)):
            record = session()
            shot = located_shot(.5)
            mutate(shot)
            record["shots"] = [shot]
            with self.subTest(shot=shot), self.assertRaises(ValueError):
                analyze_record(record)

    def test_tracking_with_no_clicks_does_not_imply_center_hits(self):
        record = session(drill="tracking")
        record["samples"] = [sample(i * .05) for i in range(21)]
        report = analyze_record(record)
        self.assertFalse(report["shot_placement"]["available"])
        self.assertIsNone(report["metrics"]["hit_position_coverage_pct"]["value"])
        self.assertIsNone(report["metrics"]["center_hit_pct"]["value"])


if __name__ == '__main__':
    unittest.main()
