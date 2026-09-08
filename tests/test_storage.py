import copy
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from companion.storage import Storage
from tests.test_metrics import sample, session


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = Storage(self.temp.name)
        self.record = session()
        self.record["samples"] = [sample(i * .05) for i in range(21)]

    def test_round_trip_survives_reopening_and_retry(self):
        report = self.storage.save_record(self.record)
        self.assertEqual(self.storage.save_record(self.record), report)
        reopened = Storage(self.temp.name)
        self.assertEqual(reopened.get_session(self.record["id"]), self.record)
        self.assertEqual(reopened.get_report(self.record["id"]), report)
        self.assertEqual(len(reopened.list_sessions()), 1)
        self.assertIsNone(reopened.get_coaching(self.record["id"]))
        result = {"cue": "Keep the same pace", "evidence_ids": ["aim-summary"]}
        reopened.save_coaching(self.record["id"], result)
        self.assertEqual(self.storage.get_coaching(self.record["id"]), result)
        self.assertTrue(self.storage.list_sessions()[0]["coaching_available"])

    def test_round_trip_when_directory_fsync_is_unavailable(self):
        with patch("companion.storage.os.O_DIRECTORY", None, create=True):
            report = self.storage.save_record(self.record)
        reopened = Storage(self.temp.name)
        self.assertEqual(reopened.get_session(self.record["id"]), self.record)
        self.assertEqual(reopened.get_report(self.record["id"]), report)

    def test_godot_reencoded_retry_has_canonical_identity_without_mutation(self):
        self.record["settings"]["seed"] = 734
        reencoded = json.loads(json.dumps(self.record), parse_int=float)
        unchanged = copy.deepcopy(reencoded)
        report = self.storage.save_record(reencoded)
        self.assertEqual(self.storage.save_record(self.record), report)
        self.assertEqual(self.storage.save_record(reencoded), report)
        stored = self.storage.get_session(self.record["id"])
        self.assertIs(type(stored["schema_version"]), int)
        self.assertIs(type(stored["settings"]["seed"]), int)
        self.assertIs(type(stored["samples"][0]["t"]), int)
        self.assertEqual(reencoded, unchanged)
        self.assertIs(type(reencoded["schema_version"]), float)
        self.assertEqual(len(self.storage.list_sessions()), 1)

    def test_normalization_still_rejects_nonfinite_and_invalid_versions_without_files(self):
        for field, value in (("schema_version", 1.2), ("schema_version", True),
                             ("seed", 734.5), ("seed", True), ("metadata", float("nan")),
                             ("metadata", float("inf"))):
            with self.subTest(field=field, value=value):
                record = copy.deepcopy(self.record)
                if field == "seed":
                    record["settings"][field] = value
                else:
                    record[field] = value
                with self.assertRaises(ValueError):
                    self.storage.save_record(record)
        self.assertEqual(self.storage.list_sessions(), [])
        self.assertEqual(list(self.storage.trace_dir.iterdir()), [])

    def test_duplicate_id_cannot_replace_existing_trace(self):
        self.storage.save_record(self.record)
        modified = copy.deepcopy(self.record)
        modified["settings"]["fov"] = 90
        with self.assertRaises(ValueError):
            self.storage.save_record(modified)
        self.assertEqual(self.storage.get_session(self.record["id"]), self.record)

    def test_failed_database_insert_leaves_no_half_saved_session(self):
        with self.storage._connect() as connection:
            connection.execute("CREATE TRIGGER reject_session BEFORE INSERT ON sessions BEGIN SELECT RAISE(ABORT, 'injected write failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.storage.save_record(self.record)
        self.assertEqual(list(self.storage.trace_dir.iterdir()), [])
        self.assertEqual(self.storage.list_sessions(), [])
        with self.storage._connect() as connection:
            connection.execute("DROP TRIGGER reject_session")
        self.storage.save_record(self.record)
        self.assertEqual(self.storage.get_session(self.record["id"]), self.record)

    def test_history_excludes_current_future_and_incomparable_rounds(self):
        for name, timestamp, fov, seed in (("earlier", 900, 103, 13), ("changed", 950, 90, 14),
                                           ("future", 1100, 103, 15), ("current", 1000, 103, 16)):
            record = copy.deepcopy(self.record)
            record.update(id=name, started_at=timestamp)
            record["settings"].update(fov=fov, seed=seed)
            report = self.storage.save_record(record)
        history = self.storage.matching_history(report)
        self.assertEqual([r["record_id"] for r in history], ["earlier"])
        self.assertIn("evidence", history[0])
        self.assertEqual(self.storage.list_sessions(limit=1)[0]["record_id"], "future")

    def test_bad_ids_and_missing_coaching_fail_without_files(self):
        for bad in ("../../outside", "/tmp/trace", "x/y", "x\\y", ""):
            with self.assertRaises(ValueError):
                self.storage.get_session(bad)
            modified = copy.deepcopy(self.record)
            modified["id"] = bad
            with self.assertRaises(ValueError):
                self.storage.save_record(modified)
        with self.assertRaises(KeyError):
            self.storage.save_coaching("missing", {"cue": "test"})
        self.assertEqual(self.storage.list_sessions(), [])
        self.assertEqual(list(Path(self.temp.name).glob("traces/*")), [])

    def test_corrupted_trace_is_reported_instead_of_silently_loaded(self):
        self.storage.save_record(self.record)
        (self.storage.trace_dir / f'{self.record["id"]}.json.gz').write_bytes(b"broken")
        with self.assertRaises(RuntimeError):
            self.storage.get_session(self.record["id"])
        self.assertIsNotNone(self.storage.get_report(self.record["id"]))

    def test_training_links_survive_reload_and_canonical_retry(self):
        baseline = self.storage.save_record(self.record)
        retest = copy.deepcopy(self.record)
        retest.update(id="retest", started_at=1001,
                      training_context={"kind": "retest", "cycle_id": "cycle-1", "baseline_record_id": self.record["id"],
                                        "source_coaching_record_id": self.record["id"], "block_index": 2})
        report = self.storage.save_record(retest)
        self.assertEqual(report["benchmark_key"], baseline["benchmark_key"])
        self.assertEqual(self.storage.save_record(json.loads(json.dumps(retest), parse_int=float)), report)
        reopened = Storage(self.temp.name)
        self.assertEqual(reopened.get_report("retest")["training_context"], retest["training_context"])
        self.assertEqual(reopened.list_sessions()[0]["training_context"], retest["training_context"])
        self.assertEqual(reopened.get_session(self.record["id"]), self.record)

    def test_invalid_training_links_fail_before_writing_traces(self):
        self.storage.save_record(self.record)
        for kind, baseline_id, source_id, started_at, drill, fov in (
            ("retest", "missing", "", 1001, "clicking", 103),
            ("practice", "test-round", "missing", 1001, "clicking", 103),
            ("retest", "test-round", "", 999, "clicking", 103),
            ("retest", "test-round", "", 1001, "tracking", 103),
            ("retest", "test-round", "", 1001, "clicking", 90),
        ):
            with self.subTest(kind=kind, baseline=baseline_id, source=source_id, started_at=started_at, drill=drill, fov=fov):
                record = copy.deepcopy(self.record)
                record.update(id="invalid", started_at=started_at, drill=drill,
                              training_context={"kind": kind, "baseline_record_id": baseline_id, "source_coaching_record_id": source_id})
                record["settings"]["fov"] = fov
                with self.assertRaises(ValueError):
                    self.storage.save_record(record)
                self.assertEqual(len(list(self.storage.trace_dir.iterdir())), 1)
        practice = copy.deepcopy(self.record)
        practice.update(id="practice", started_at=1001,
                        training_context={"kind": "practice", "baseline_record_id": self.record["id"]})
        practice["settings"]["target_scale"] = 1.2
        self.storage.save_record(practice)

    def test_coaching_journal_preserves_prior_questions_and_excludes_future_rounds(self):
        original_report = self.storage.save_record(self.record)
        first = {"cue": "Land first", "success_check": "Accuracy at least 95%"}
        context = {"comparison": {"baseline_record_id": "older"}}
        self.storage.save_coaching(self.record["id"], first, "What should I focus on?", context)
        self.storage.save_coaching(self.record["id"], {"cue": "Keep that improvement"}, "What changed?")
        future = copy.deepcopy(self.record)
        future.update(id="future", started_at=1100)
        self.storage.save_record(future)
        self.storage.save_coaching("future", {"cue": "Future advice"})
        current = copy.deepcopy(self.record)
        current.update(id="current", started_at=1050)
        report = self.storage.save_record(current)
        history = Storage(self.temp.name).coaching_history(report)
        self.assertEqual([row["result"]["cue"] for row in history], ["Keep that improvement", "Land first"])
        self.assertEqual(history[1]["question"], "What should I focus on?")
        self.assertEqual(history[1]["context"], context)
        self.assertEqual(history[1]["benchmark_key"], original_report["benchmark_key"])
        self.assertEqual(self.storage.get_coaching(self.record["id"])["cue"], "Keep that improvement")
        self.assertEqual(len(self.storage.coaching_history(original_report)), 2)

    def test_linked_cross_drill_advice_is_retained_when_recent_history_is_full(self):
        source = copy.deepcopy(self.record)
        source.update(id="source", started_at=900, drill="switching")
        self.storage.save_record(source)
        self.storage.save_coaching("source", {"cue": "Original prescription"})
        for i in range(4):
            record = copy.deepcopy(self.record)
            record.update(id=f"prior-{i}", started_at=950 + i)
            self.storage.save_record(record)
            self.storage.save_coaching(record["id"], {"cue": f"Later advice {i}"})
        self.record["training_context"] = {"kind": "practice", "source_coaching_record_id": "source"}
        report = self.storage.save_record(self.record)
        history = self.storage.coaching_history(report, limit=3)
        self.assertEqual([row["record_id"] for row in history], ["prior-3", "prior-2", "source"])
        self.assertEqual(history[-1]["drill"], "switching")

    def test_legacy_coaching_is_backfilled_once_without_rewriting_user_records(self):
        original_report = self.storage.save_record(self.record)
        result = {"cue": "Existing advice"}
        self.storage.save_coaching(self.record["id"], result)
        trace_path = self.storage.trace_dir / f'{self.record["id"]}.json.gz'
        trace_bytes = trace_path.read_bytes()
        with self.storage._connect() as connection:
            connection.execute("DROP TABLE coaching_journal")
            connection.execute("DROP TABLE storage_migrations")
        reopened = Storage(self.temp.name)
        self.assertEqual(reopened.coaching_history(original_report)[0]["result"], result)
        self.assertEqual(len(Storage(self.temp.name).coaching_history(original_report)), 1)
        self.assertEqual(reopened.get_report(self.record["id"]), original_report)
        self.assertEqual(trace_path.read_bytes(), trace_bytes)
        self.assertEqual(reopened.get_session(self.record["id"]), self.record)
        reopened.save_coaching(self.record["id"], {"cue": "New advice"})
        self.assertEqual(len(Storage(self.temp.name).coaching_history(original_report)), 2)

    def test_sensitivity_experiments_persist_updates_without_touching_sessions(self):
        experiment = {"id": "sensitivity-1", "status": "running", "candidates": [.025, .02125], "blocks": []}
        self.storage.save_sensitivity_experiment(experiment)
        experiment["status"] = "complete"
        experiment["blocks"] = [{"record_id": "round-1", "index": 0.0}]
        self.storage.save_sensitivity_experiment(experiment)
        reopened = Storage(self.temp.name)
        self.assertEqual(reopened.get_sensitivity_experiment(experiment["id"]), experiment)
        self.assertEqual(reopened.list_sensitivity_experiments(), [experiment])
        self.assertEqual(reopened.list_sessions(), [])
        for bad in ({"id": "../outside"}, {"id": "nan", "value": float("nan")}, []):
            with self.assertRaises(ValueError):
                reopened.save_sensitivity_experiment(bad)
        with self.assertRaises(KeyError):
            reopened.get_sensitivity_experiment("missing")
        self.assertEqual(len(reopened.list_sensitivity_experiments()), 1)


if __name__ == "__main__":
    unittest.main()
