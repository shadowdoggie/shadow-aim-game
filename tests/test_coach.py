import copy
import http.client
import io
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from companion.coach import (CoachCancelled, CoachError, CodexCoach, EFFORT,
    MODEL, _command, validate_recommendation)
from companion.auth import AuthError
from companion.server import CompanionServer, JobManager, benchmark_comparison


def report():
    return {"record_id": "record-1", "drill": "clicking", "started_at": "2026-09-08T20:00:00Z",
        "benchmark_key": "clicking-v1", "quality": {"usable_for_coaching": True},
        "evidence": [{"id": "record-1:shots", "summary": "40 hits from 50 shots"}]}


def recommendation():
    return {"summary": "Use one deliberate stop before each click.",
        "observation": "40 of 50 shots hit the target.", "evidence_ids": ["record-1:shots"],
        "cue": "Stop, then click.", "drill": "clicking",
        "parameters": {"duration_s": 45, "target_scale": 1, "speed_scale": 1},
        "success_criterion": "Raise accuracy above 80% without increasing acquisition time.",
        "confidence": "medium", "needs_more_data": False}


class ValidationTests(unittest.TestCase):
    def test_accepts_recorded_evidence_and_returns_copy(self):
        value = recommendation()
        self.assertEqual(validate_recommendation(value, report(), []), value)
        self.assertIsNot(validate_recommendation(value, report(), []), value)

    def test_rejects_invented_evidence(self):
        value = recommendation()
        value["evidence_ids"] = ["invented"]
        with self.assertRaisesRegex(CoachError, "not recorded"):
            validate_recommendation(value, report(), [])

    def test_accepts_other_baseline_evidence(self):
        value = recommendation()
        value["evidence_ids"] = ["tracking-1:error"]
        validate_recommendation(value, report(), [{"drill": "clicking",
            "quality": {"usable_for_coaching": True}, "evidence": [{"id": "tracking-1:error"}]}])

    def test_limited_record_requires_low_confidence_and_more_data(self):
        limited = report()
        limited["quality"]["usable_for_coaching"] = False
        with self.assertRaisesRegex(CoachError, "overstated"):
            validate_recommendation(recommendation(), limited, [])
        value = recommendation()
        value.update(confidence="low", needs_more_data=True)
        validate_recommendation(value, limited, [])

    def test_valid_other_baseline_can_support_coaching_when_last_drill_is_limited(self):
        limited = report()
        limited["quality"]["usable_for_coaching"] = False
        value = recommendation()
        value["drill"] = "tracking"
        value["evidence_ids"] = ["tracking-1:error"]
        validate_recommendation(value, limited, [{"drill": "tracking",
            "quality": {"usable_for_coaching": True}, "evidence": [{"id": "tracking-1:error"}]}])

    def test_rejects_invalid_drills_numbers_and_shape(self):
        mutations = [lambda x: x.update(drill="execute-code"),
            lambda x: x["parameters"].update(duration_s=True),
            lambda x: x["parameters"].update(target_scale=float("nan")),
            lambda x: x["parameters"].update(speed_scale=3),
            lambda x: x.update(command="touch file"),
            lambda x: x.update(evidence_ids=[])]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                value = recommendation()
                mutate(value)
                with self.assertRaises(CoachError):
                    validate_recommendation(value, report(), [])


class ProtocolCoach(CodexCoach):
    def __init__(self, final_text=None, model=MODEL, effort=EFFORT):
        super().__init__()
        self.sent = []
        self.confirmed_model = model
        self.confirmed_effort = effort
        self.final_text = final_text or json.dumps(recommendation())
        self._temporary = type("Directory", (), {"name": "/tmp", "cleanup": lambda _: None})()

    def connect(self, cancel=None):
        self.state = "ready"

    def _request(self, method, params, cancel=None, timeout=30):
        self.sent.append((method, params))
        if method == "thread/start":
            return {"model": self.confirmed_model, "reasoningEffort": self.confirmed_effort, "thread": {"id": "thread-1"}}
        if method == "turn/start":
            self._events.put({"method": "item/completed", "params": {"threadId": "thread-1",
                "item": {"type": "agentMessage", "text": self.final_text}}})
            self._events.put({"method": "turn/completed", "params": {"threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "completed"}}})
            return {"turn": {"id": "turn-1"}}
        return {}


class ProtocolTests(unittest.TestCase):
    def test_requests_exact_model_medium_and_no_environments(self):
        coach = ProtocolCoach()
        self.assertEqual(coach.recommend(report()), recommendation())
        thread, turn = coach.sent
        self.assertEqual(thread[1]["model"], MODEL)
        self.assertFalse(thread[1]["allowProviderModelFallback"])
        self.assertEqual(turn[1]["model"], MODEL)
        self.assertEqual(turn[1]["effort"], "medium")
        self.assertEqual(turn[1]["environments"], [])
        self.assertEqual(turn[1]["approvalPolicy"], "never")
        self.assertIn("outputSchema", turn[1])

    def test_model_substitution_fails_before_inference(self):
        coach = ProtocolCoach(model="other")
        with self.assertRaisesRegex(CoachError, "did not confirm"):
            coach.recommend(report())
        self.assertEqual(len(coach.sent), 1)

    def test_effort_substitution_fails_before_inference(self):
        coach = ProtocolCoach(effort="high")
        with self.assertRaisesRegex(CoachError, "did not confirm gpt-5.6-sol with medium effort"):
            coach.recommend(report())
        self.assertEqual(len(coach.sent), 1)

    def test_malformed_model_json_fails(self):
        coach = ProtocolCoach(final_text="Not JSON")
        with self.assertRaisesRegex(CoachError, "malformed"):
            coach.recommend(report())

    def test_cancel_while_waiting(self):
        coach = CodexCoach()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(CoachCancelled):
            coach._next_event(time.monotonic() + 1, cancel)

    def test_disconnected_process_is_clear_failure(self):
        coach = CodexCoach()
        coach._events.put({"_closed": True})
        with self.assertRaisesRegex(CoachError, "disconnected"):
            coach._next_event(time.monotonic() + 1)

    def test_command_disables_tools_and_forces_subscription(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "config.toml").write_text('[mcp_servers.sample]\ncommand="example"\n')
            with patch.dict("os.environ", {"CODEX_HOME": directory}):
                command = _command("codex")
        self.assertIn('forced_login_method="chatgpt"', command)
        self.assertIn('mcp_servers.sample.enabled=false', command)
        self.assertIn('features.shell_tool=false', command)
        self.assertIn('features.plugins=false', command)
        self.assertIn('project_doc_max_bytes=0', command)

    def test_reconnect_uses_fresh_selected_auth_environment_and_config(self):
        class FakeProcess:
            def __init__(self):
                self.stdin, self.stdout = io.StringIO(), io.StringIO()
                self.exited = False

            def poll(self):
                return 0 if self.exited else None

            def terminate(self):
                self.exited = True

            def wait(self, timeout=None):
                return 0

        with tempfile.TemporaryDirectory() as directory:
            global_home = Path(directory, 'global')
            private_home = Path(directory, 'private')
            for folder, server in ((global_home, 'global_only'), (private_home, 'private_only')):
                folder.mkdir()
                (folder / 'config.toml').write_text(f'[mcp_servers.{server}]\ncommand="example"\n')
            first = {'CODEX_HOME': str(global_home), 'OPENAI_API_KEY': 'test-only-api-key', 'PATH': '/bin'}
            second = {'CODEX_HOME': str(private_home), 'CODEX_API_KEY': 'test-only-api-key', 'PATH': '/bin'}
            factory = Mock(side_effect=[first, second])
            coach = CodexCoach(binary='codex', env_factory=factory)
            responses = [{}, {'account': {'type': 'chatgpt'}}, {'data': [{'model': MODEL,
                'supportedReasoningEfforts': [{'reasoningEffort': EFFORT}]}]}]
            with patch.dict('os.environ', {'CODEX_HOME': str(global_home)}), \
                    patch('companion.coach.subprocess.Popen', side_effect=lambda *a, **k: FakeProcess()) as popen, \
                    patch.object(coach, '_request', side_effect=responses * 2):
                coach.connect()
                coach.close()
                coach.connect()
                coach.close()
            self.assertEqual(factory.call_count, 2)
            calls = popen.call_args_list
            self.assertEqual(calls[0].kwargs['env']['CODEX_HOME'], str(global_home))
            self.assertEqual(calls[1].kwargs['env']['CODEX_HOME'], str(private_home))
            self.assertNotIn('OPENAI_API_KEY', calls[0].kwargs['env'])
            self.assertNotIn('CODEX_API_KEY', calls[1].kwargs['env'])
            self.assertIn('mcp_servers.private_only.enabled=false', calls[1].args[0])
            self.assertNotIn('mcp_servers.global_only.enabled=false', calls[1].args[0])
            self.assertIn('OPENAI_API_KEY', first)  # Factory-owned dictionaries are not changed.
            self.assertIn('CODEX_API_KEY', second)

    def test_invalid_selected_auth_scope_fails_before_process_launch(self):
        coach = CodexCoach(env_factory=Mock(side_effect=AuthError('The saved account selection is invalid.')))
        with patch('companion.coach.subprocess.Popen') as popen:
            with self.assertRaisesRegex(CoachError, 'saved account selection'):
                coach.connect()
        popen.assert_not_called()
        self.assertEqual(coach.state, 'unavailable')
        self.assertIsNone(coach._temporary)


class FakeStorage:
    def __init__(self, data_dir=None):
        self.saved = {}
        if data_dir is not None:
            self.data_dir = Path(data_dir)

    def get_report(self, identifier):
        if identifier != "record-1":
            raise KeyError(identifier)
        return report()

    def get_coaching(self, identifier):
        return self.saved.get(identifier)

    def matching_history(self, _report, limit=5):
        return []

    def list_sessions(self, limit=30):
        return [report()]

    def baseline_history(self, limit=100):
        return [item for item in self.list_sessions(limit) if item.get("training_context", {}).get("kind") == "baseline"]

    def save_coaching(self, identifier, value, question="", context=None):
        self.saved[identifier] = value

    def coaching_history(self, _report, limit=6):
        return []


class FakeCoach:
    def __init__(self, block=False, fail=False):
        self.state = "ready"
        self.last_error = None
        self.block = block
        self.fail = fail
        self.started = threading.Event()
        self.closed = False

    def recommend(self, report, history, question, cancel, previous_coaching=None, coaching_context=None):
        self.started.set()
        if self.block:
            cancel.wait(2)
            if cancel.is_set():
                raise CoachCancelled("Coaching cancelled.")
        if self.fail:
            raise CoachError("Codex disconnected. Retry to reconnect.")
        return recommendation()

    def close(self):
        self.closed = True


def wait_done(manager, job_id):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = manager.get(job_id)
        if result["status"] != "pending":
            return result
        time.sleep(.01)
    raise AssertionError("Job did not finish")


class JobTests(unittest.TestCase):
    def test_complete_persists_result(self):
        storage = FakeStorage()
        manager = JobManager(storage, FakeCoach(), preconnect=False)
        try:
            result = wait_done(manager, manager.submit("record-1"))
            self.assertEqual(result["status"], "complete")
            self.assertEqual(storage.saved["record-1"], recommendation())
        finally:
            manager.close()

    def test_cancel_active_and_duplicate_request(self):
        storage, coach = FakeStorage(), FakeCoach(block=True)
        manager = JobManager(storage, coach, preconnect=False)
        try:
            identifier = manager.submit("record-1")
            self.assertTrue(coach.started.wait(1))
            self.assertEqual(manager.submit("record-1"), identifier)
            self.assertEqual(manager.cancel(identifier)["status"], "cancelled")
            self.assertEqual(wait_done(manager, identifier)["status"], "cancelled")
        finally:
            manager.close()
        self.assertEqual(storage.saved, {})
        self.assertTrue(coach.closed)

    def test_connection_failure_has_no_fake_result(self):
        manager = JobManager(FakeStorage(), FakeCoach(fail=True), preconnect=False)
        try:
            result = wait_done(manager, manager.submit("record-1"))
            self.assertEqual(result["status"], "failed")
            self.assertIsNone(result["result"])
            self.assertIn("disconnected", result["error"])
        finally:
            manager.close()

    def test_unknown_record_rejected_before_queue(self):
        manager = JobManager(FakeStorage(), FakeCoach(), preconnect=False)
        try:
            with self.assertRaises(KeyError):
                manager.submit("unknown")
        finally:
            manager.close()


class ComparisonTests(unittest.TestCase):
    def test_returns_current_minus_previous_for_matching_metrics(self):
        storage = FakeStorage()
        previous = report()
        previous["record_id"] = "older"
        previous["metrics"] = {"accuracy_pct": {"value": 80, "unit": "%"},
            "acquisition_ms": {"value": None, "unit": "ms"}}
        current = report()
        current["metrics"] = {"accuracy_pct": {"value": 90, "unit": "%"},
            "acquisition_ms": {"value": 500, "unit": "ms"}}
        storage.matching_history = lambda _report, limit: [previous]
        comparison = benchmark_comparison(storage, current)
        self.assertEqual(comparison["previous_record_id"], "older")
        self.assertEqual(comparison["metrics"]["accuracy_pct"]["delta"], 10)
        self.assertNotIn("acquisition_ms", comparison["metrics"])

    def test_first_benchmark_has_no_comparison(self):
        self.assertIsNone(benchmark_comparison(FakeStorage(), report()))


class HttpTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        storage = FakeStorage(data_dir=directory.name)
        self.manager = JobManager(storage, FakeCoach(), preconnect=False)
        self.token = "x" * 32
        self.server = CompanionServer(("127.0.0.1", 0), self.token, storage, self.manager)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.manager.close()
        self.worker.join(1)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        status, value = response.status, json.loads(response.read())
        connection.close()
        return status, value

    def test_health_auth_and_browser_origin(self):
        self.assertEqual(self.request("GET", "/health")[0], 401)
        headers = {"Authorization": "Bearer " + self.token}
        status, result = self.request("GET", "/health", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual((result["model"], result["effort"]), (MODEL, "medium"))
        headers["Origin"] = "https://example.com"
        self.assertEqual(self.request("GET", "/health", headers=headers)[0], 401)

    def test_malformed_json_is_bad_request(self):
        status, result = self.request("POST", "/coach", body="{", headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertIn("JSON", result["error"])


if __name__ == "__main__":
    unittest.main()
