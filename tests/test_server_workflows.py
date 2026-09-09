import copy
import http.client
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from companion.server import CompanionServer, JobManager
from companion.sensitivity import add_result, analyze_experiment, create_experiment
from companion.storage import Storage
from companion.training import benchmark_comparison, coaching_context, contextual_history
from companion.voice_bridge import VoiceError
from tests.test_coach import wait_done
from tests.test_metrics import event, sample, session
from tests.test_sensitivity import SETTINGS, measured_round


def clicking_record(identifier, timestamp, hits=90, acquisition=.3, kind="free", baseline_id="", source_id=""):
    record = session(identifier, duration=45)
    record["started_at"] = timestamp
    record["training_context"] = {"kind": kind, "cycle_id": "cycle-1"}
    if baseline_id:
        record["training_context"]["baseline_record_id"] = baseline_id
    if source_id:
        record["training_context"]["source_coaching_record_id"] = source_id
    record["samples"] = [sample(i * .02, target_id=f"target-{min(99, int(i * .02 / .44))}") for i in range(2251)]
    for index in range(100):
        target_id = f"target-{index}"
        record["events"].append(event(index * .44, "spawn", target_id, yaw=0))
        record["shots"].append({"t": index * .44 + acquisition, "target_id": target_id,
                                "hit": index < hits, "error_deg": 0 if index < hits else 3})
    return record


class RecordingCoach:
    state = "ready"
    last_error = None

    def __init__(self):
        self.calls = []
        self.reviews = []
        self.closed = False

    def recommend(self, report, history, question, cancel, previous_coaching=None, coaching_context=None):
        self.calls.append(copy.deepcopy({"report": report, "history": history, "question": question,
                                         "context": coaching_context, "previous": previous_coaching}))
        return {"cue": "Use the improved accuracy to try a modest pace increase.",
                "progress_summary": "Accuracy improved from 90% to 97% at the original settings.",
                "drill": "clicking", "parameters": {"duration_s": 45}, "goals": []}

    def review_sensitivity(self, analysis, cancel=None):
        self.reviews.append(copy.deepcopy(analysis))
        return {"summary": "These repeats support keeping your current setting.",
                "reason": "Neither alternative improved both repeats.",
                "recommended_candidate_id": "current", "confidence": "low",
                "evidence_ids": [analysis["evidence"][0]["id"]],
                "next_step": "Keep this setting and recheck next session."}

    def close(self):
        self.closed = True


class FakeMusic:
    def __init__(self):
        self.tracks = [{"id": "song-1", "title": "First song", "artist": "First artist", "album": "Album"},
                       {"id": "song-2", "title": "Another song", "artist": "Other artist", "album": "Album"}]
        self.scans = []
        self.prepared = []
        self.scan_status = "ready"

    def progress(self):
        return {"folder_selected": True, "status": self.scan_status, "total": len(self.tracks), "scanned": len(self.tracks), "skipped": 0, "error": None, "complete": self.scan_status == "ready"}

    def select_mood(self, mood, limit=100):
        return {"tracks": self.tracks, "scan": self.progress(), "selection_label": "Relaxing music"}

    def select_mix(self, query, limit=100):
        return {"tracks": self.tracks, "scan": self.progress(), "selection_label": query.capitalize() + " music"}

    def continuation(self, identifier, limit=100):
        return {"tracks": [self.get_track(identifier)], "selection_label": "Selected song", "scan": self.progress()}

    def status(self):
        return {"root": "", "status": "idle", "tracks": self.tracks, "total": len(self.tracks), "error": None}

    def start_scan(self, path):
        self.scans.append(path)
        return self.status() | {"root": path, "status": "scanning"}

    def search(self, query, limit=50):
        tracks = [] if query == "missing" else self.tracks if query == "ambiguous" else self.tracks[:1]
        return {"tracks": tracks, "total": len(tracks), "ambiguous": len(tracks) > 1,
                "exact_match_id": tracks[0]["id"] if len(tracks) == 1 else None}

    def get_track(self, identifier):
        return next(track for track in self.tracks if track["id"] == identifier)

    def prepare(self, identifier):
        self.prepared.append(identifier)
        return {"track": self.get_track(identifier), "path": "/indexed/selected-song.wav", "format": "wav"}

    def close(self):
        pass


class FakeAuth:
    def __init__(self, quiesced):
        self.state = "signed_in"
        self.actions = []
        self.quiesced = quiesced

    def status(self):
        return {"state": self.state, "account": {"email": "player@example.test", "plan_type": "plus"} if self.state == "signed_in" else None,
                "login": {"login_id": "login-1", "url": "https://auth.openai.com/test-login"} if self.state == "pending" else None,
                "source": "app", "error": None}

    def start_login(self, flow="browser"):
        self.actions.append(("login", flow, self.quiesced()))
        self.state = "pending"
        return self.status()

    def cancel_login(self):
        self.actions.append(("cancel",))
        self.state = "signed_out"
        return self.status()

    def logout(self):
        self.actions.append(("logout", self.quiesced()))
        self.state = "signed_out"
        return self.status()

    def close(self):
        pass


class FakeVoice:
    def __init__(self):
        self.state = "off"
        self.starts = []
        self.updates = []
        self.audio = []
        self.cursors = []
        self.live_updates = []
        self.failure = False
        self.closed = False

    def status(self):
        return {"state": self.state, "model": "gpt-live-1-codex", "voice": "juniper"}

    def start(self, context=None):
        self.starts.append(context)
        if self.failure:
            raise VoiceError("Voice account access is unavailable.")
        self.state = "connected"
        return self.status()

    def stop(self):
        self.state = "off"
        return self.status()

    def update_context(self, context, speak=False):
        self.updates.append((copy.deepcopy(context), speak))
        return self.status()

    def update_live_state(self, context):
        self.live_updates.append(copy.deepcopy(context))
        return self.status()

    def push_audio(self, audio, sample_rate=24000, num_channels=1):
        if audio != "AAA=" or sample_rate != 24000 or num_channels != 1:
            raise VoiceError("Invalid audio format.")
        self.audio.append(audio)
        return {"accepted": True, "state": self.state}

    def events(self, after=0):
        self.cursors.append(after)
        return {**self.status(), "events": [{"seq": after + 1, "type": "interrupt"}], "latest_seq": after + 1}

    def close(self):
        self.closed = True


class ServerWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(self.temp.name)
        self.coach = RecordingCoach()
        self.jobs = JobManager(self.storage, self.coach, preconnect=False)
        self.voice = FakeVoice()
        self.token = "workflow-test-token-" * 2
        self.server = CompanionServer(("127.0.0.1", 0), self.token, self.storage, self.jobs, voice=self.voice)
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.jobs.close()
        self.worker.join(1)
        self.temp.cleanup()

    def request(self, method, path, value=None, authenticated=True):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.token
        body = json.dumps(value) if value is not None else None
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        status, value = response.status, json.loads(response.read())
        connection.close()
        return status, value

    def run_action(self, name, arguments):
        result = {}
        finished = threading.Event()
        def call():
            result.update(self.server.voice_action(name, arguments))
            finished.set()
        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        return result, finished, worker

    def next_action(self, after=0):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status, pending = self.request("GET", "/controls?after=" + str(after))
            self.assertEqual(status, 200)
            if pending["actions"]:
                return pending["actions"][0]
            time.sleep(.005)
        self.fail("No native action was queued")

    def training_cycle(self):
        baseline = self.storage.save_record(clicking_record("baseline", 1000, kind="baseline"))
        advice = {"cue": "Slow your approach", "drill": "clicking", "parameters": {"duration_s": 45},
                  "goals": [{"metric": "accuracy_pct", "operator": "at_least", "value": 95}]}
        self.storage.save_coaching("baseline", advice, "Help my aim", {"comparison": None})
        self.storage.save_record(clicking_record("practice", 1100, hits=98, acquisition=.2,
            kind="practice", baseline_id="baseline", source_id="baseline"))
        retest = self.storage.save_record(clicking_record("retest", 1200, hits=97, acquisition=.25,
            kind="retest", baseline_id="baseline", source_id="baseline"))
        return baseline, retest

    def reviewed_sensitivity(self, identifier="reviewed-test", base_sensitivity=.07):
        experiment = create_experiment(SETTINGS | {"sensitivity_deg_per_count": base_sensitivity})
        experiment.update(id=identifier, created_at=1400)
        reports = []
        for index in range(len(experiment["blocks"])):
            record = measured_round(experiment, index)
            record.update(id=f"{identifier}-{index}", started_at=1400 + 25 * index)
            report = self.storage.save_record(record)
            reports.append(report)
            experiment = add_result(experiment, record, report)
        experiment.update(analysis=analyze_experiment(experiment, reports), reviewed_at=1800,
            review={"summary": "The short test is inconclusive.", "reason": "Lower sensitivity improved clicking accuracy from 95% to 97%, but tracking error rose from 2.0 to 2.8 degrees.",
                    "recommended_candidate_id": "current", "confidence": "low",
                    "next_step": "Keep the tested base setting for now and repeat the comparison next session."})
        self.storage.save_sensitivity_experiment(experiment)
        return experiment

    def test_ui_and_coach_share_original_baseline_even_when_practice_scored_higher(self):
        baseline, retest = self.training_cycle()
        status, result = self.request("GET", "/sessions/retest")
        self.assertEqual(status, 200)
        comparison = result["comparison"]
        self.assertEqual(comparison["previous_record_id"], "baseline")
        self.assertEqual(comparison["metrics"]["accuracy_pct"],
                         {"previous": 90, "current": 97, "delta": 7, "unit": "%"})
        status, pending = self.request("POST", "/coach", {"record_id": "retest", "question": "Why repeat the same cue?"})
        self.assertEqual(status, 200)
        self.assertEqual(wait_done(self.jobs, pending["job_id"])["status"], "complete")
        call = self.coach.calls[-1]
        self.assertEqual(call["context"]["comparison"], comparison)
        self.assertEqual(call["context"]["journal"][0]["result"]["cue"], "Slow your approach")
        self.assertEqual({item["record_id"]: item["comparison_role"] for item in call["history"]},
                         {"practice": "practice", "baseline": "same_benchmark"})
        reopened = Storage(self.temp.name)
        self.assertEqual(benchmark_comparison(reopened, retest), comparison)
        journal = reopened.coaching_history(retest)
        self.assertEqual(journal[0]["question"], "Why repeat the same cue?")
        self.assertEqual(journal[0]["context"]["comparison"], comparison)
        self.assertNotIn("journal", journal[0]["context"])
        self.assertEqual(self.voice.updates, [])

    def test_ordinary_history_does_not_learn_from_sensitivity_or_use_practice_as_baseline(self):
        self.training_cycle()
        sensitivity = clicking_record("screening", 1250, hits=100, kind="sensitivity")
        screening_report = self.storage.save_record(sensitivity)
        self.storage.save_coaching("screening", {"cue": "Legacy isolated screening advice"})
        current = self.storage.save_record(clicking_record("free", 1300, hits=96))
        self.assertEqual(benchmark_comparison(self.storage, current)["previous_record_id"], "retest")
        self.assertNotIn("screening", [item["record_id"] for item in contextual_history(self.storage, current)])
        self.assertNotIn("screening", [item["record_id"] for item in coaching_context(self.storage, current)["journal"]])
        self.assertIsNone(benchmark_comparison(self.storage, screening_report))
        self.assertIsNone(benchmark_comparison(self.storage, self.storage.get_report("practice")))
        self.assertEqual(self.request("POST", "/coach", {"record_id": "screening"})[0], 400)
        self.assertEqual(self.coach.calls, [])

    def test_sensitivity_endpoints_preserve_trials_reject_wrong_assignments_and_persist_review(self):
        status, value = self.request("POST", "/sensitivity/start", {"settings": SETTINGS})
        self.assertEqual(status, 200)
        experiment = value["experiment"]
        prefix = "/sensitivity/" + experiment["id"]
        self.assertEqual(self.request("POST", prefix + "/review", {})[0], 400)
        skipped = measured_round(experiment, 1)
        self.assertEqual(self.request("POST", prefix + "/round", {"record": skipped, "block_index": 1})[0], 400)
        self.assertEqual(self.storage.list_sessions(), [])
        for index in range(len(experiment["blocks"])):
            record = measured_round(experiment, index)
            status, state = self.request("POST", prefix + "/round", {"record": record, "block_index": index})
            self.assertEqual(status, 200, state)
            if index == 0:
                status, retried = self.request("POST", prefix + "/round", {"record": record, "block_index": index})
                self.assertEqual(status, 200)
                self.assertEqual(retried["experiment"]["record_ids"], [record["id"]])
        status, state = self.request("GET", prefix)
        self.assertEqual((status, state["experiment"]["status"]), (200, "complete"))
        self.assertEqual(state["analysis"]["status"], "inconclusive")
        status, pending = self.request("POST", prefix + "/review", {})
        self.assertEqual(status, 200)
        finished = wait_done(self.jobs, pending["job_id"])
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["result"]["review"]["recommended_candidate_id"], "current")
        reopened = Storage(self.temp.name)
        saved = reopened.get_sensitivity_experiment(experiment["id"])
        self.assertEqual(saved["review"], finished["result"]["review"])
        self.assertEqual(saved["base_settings"]["sensitivity_deg_per_count"], SETTINGS["sensitivity_deg_per_count"])
        self.assertEqual(len(reopened.list_sessions()), len(experiment["blocks"]))
        self.assertEqual(self.coach.calls, [])
        self.assertEqual(self.voice.updates, [])

    def test_voice_starts_only_on_request_and_uses_persisted_context_with_plain_speech(self):
        self.training_cycle()
        advice = self.coach.recommend({}, [], "", None)
        self.storage.save_coaching("retest", advice)
        self.assertEqual(self.request("GET", "/voice/status")[1]["state"], "off")
        status, result = self.request("POST", "/voice/context", {"record_id": "retest", "phase": "retest", "screen": "results",
            "drill": "clicking", "playing": False, "speak": True, "coaching": {"cue": "Invented 100% accuracy"}})
        self.assertEqual(status, 200)
        context, spoken = self.voice.updates
        self.assertFalse(context[1])
        self.assertEqual(context[0]["comparison"]["previous_record_id"], "baseline")
        self.assertEqual(context[0]["coaching"], advice)
        self.assertTrue(spoken[1])
        self.assertIsInstance(spoken[0], str)
        self.assertIn("improved accuracy", spoken[0])
        self.assertNotRegex(spoken[0], r"\d|%")
        self.assertNotIn("Invented", spoken[0])
        self.assertNotIn("record_id", spoken[0])
        self.assertLessEqual(len(spoken[0].split()), 80)
        self.assertEqual(self.voice.starts, [])
        self.assertEqual(self.request("POST", "/voice/start", {})[1]["state"], "connected")
        self.assertEqual(self.voice.starts[0]["record_id"], "retest")
        self.assertEqual(self.request("POST", "/voice/audio", {"audio": "AAA=", "sample_rate": 24000, "num_channels": 1})[0], 200)
        self.assertEqual(self.request("GET", "/voice/events?after=7")[1]["events"][0]["seq"], 8)
        self.assertEqual(self.voice.cursors, [7])
        self.assertEqual(self.request("POST", "/voice/stop", {})[1]["state"], "off")

    def test_next_training_route_validates_settings_without_account_or_inference(self):
        self.assertEqual(self.request("GET", "/training/next", authenticated=False)[0], 401)
        status, step = self.request("GET", "/training/next?settings=%7B%22fov%22%3A103%7D")
        self.assertEqual(status, 200)
        self.assertEqual(step["action"], "baseline")
        self.assertEqual(set(step["missing_modes"]), {"clicking", "tracking", "reactive_tracking", "switching"})
        self.assertEqual(self.request("GET", "/training/next?settings=%5B%5D")[0], 400)
        self.assertEqual(self.request("GET", "/training/next?settings=%7B%22fov%22%3A0%7D")[0], 400)
        self.assertEqual(self.coach.calls, [])

    def test_voice_errors_have_no_coaching_fallback_and_routes_require_auth(self):
        for path in ("/voice/start", "/sensitivity/start"):
            self.assertEqual(self.request("POST", path, {}, authenticated=False)[0], 401)
        self.assertEqual(self.voice.starts, [])
        self.voice.failure = True
        status, result = self.request("POST", "/voice/start", {})
        self.assertEqual(status, 400)
        self.assertIn("unavailable", result["error"])
        self.assertEqual(self.coach.calls, [])
        self.assertEqual(self.coach.reviews, [])
        self.assertEqual(self.request("POST", "/voice/audio", {"audio": "bad"})[0], 400)
        self.assertEqual(self.request("GET", "/voice/events?after=-1")[0], 400)
        self.assertEqual(self.request("POST", "/voice/context", {"playing": "yes"})[0], 400)
        self.assertEqual(self.request("POST", "/voice/context", {"speak": True})[0], 400)
        self.assertEqual(self.voice.audio, [])
        self.assertEqual(self.voice.cursors, [])

    def test_pending_review_announces_measured_results_once_and_requires_matching_job(self):
        self.training_cycle()
        payload = {"record_id": "retest", "screen": "results", "playing": False,
                   "speak": True, "review_pending_job_id": "pending-review"}
        with patch.object(self.jobs, "get", return_value={"status": "pending", "record_id": "retest"}):
            self.assertEqual(self.request("POST", "/voice/context", payload)[0], 200)
            spoken = self.voice.updates[-1]
            self.assertTrue(spoken[1])
            self.assertIn("next focus", spoken[0])
            self.assertNotRegex(spoken[0], r"\d|%|percent")
            count = len(self.voice.updates)
            self.assertEqual(self.request("POST", "/voice/context", payload)[0], 200)
            self.assertEqual(len(self.voice.updates), count)
            self.assertEqual(self.request("POST", "/voice/context", payload | {"record_id": "baseline"})[0], 400)
        with patch.object(self.jobs, "get", return_value={"status": "failed", "record_id": "retest"}):
            self.assertEqual(self.request("POST", "/voice/context", payload | {"review_pending_job_id": "failed-review"})[0], 200)
            self.assertEqual(len(self.voice.updates), count)

    def test_voice_keeps_review_evidence_across_navigation_and_distinguishes_profile_and_trial_settings(self):
        self.training_cycle()
        experiment = self.reviewed_sensitivity()
        profile = SETTINGS | {"sensitivity_deg_per_count": .06}
        active = SETTINGS | {"sensitivity_deg_per_count": .048}
        status, _ = self.request("POST", "/voice/context", {"record_id": "retest", "phase": "sensitivity", "screen": "results",
            "analysis": {"experiment_id": experiment["id"]}, "sensitivity_review": {"reason": "Invented result"},
            "profile_settings": profile, "active_settings": active, "speak": True})
        self.assertEqual(status, 200)
        spoken = self.voice.updates[-1]
        self.assertTrue(spoken[1])
        self.assertNotRegex(spoken[0], r"\d|%|percent|degrees")
        self.assertTrue(spoken[0])
        self.assertNotIn("Invented", spoken[0])
        self.assertLessEqual(len(spoken[0].split()), 80)
        # A newer unfinished screening must not replace the completed review.
        self.storage.save_sensitivity_experiment({"id": "unfinished", "status": "collecting", "created_at": 1600})
        status, _ = self.request("POST", "/voice/context", {"phase": "free", "screen": "training", "drill": "tracking",
            "playing": True, "profile_settings": profile, "active_settings": active})
        self.assertEqual(status, 200)
        context = self.voice.updates[-1][0]
        self.assertEqual(context["record_id"], "retest")
        self.assertEqual(context["analysis"]["experiment_id"], experiment["id"])
        self.assertEqual(context["sensitivity_review"]["reason"], experiment["review"]["reason"])
        self.assertEqual(context["sensitivity_context"]["tested_base_settings"]["sensitivity_deg_per_count"], .07)
        self.assertEqual(context["profile_settings"]["sensitivity_deg_per_count"], .06)
        self.assertEqual(context["active_settings"]["sensitivity_deg_per_count"], .048)
        self.assertEqual(context["ui_controls"]["training_tab"], "Train")
        clicking = next(card for card in context["ui_controls"]["drill_cards"] if card["drill_id"] == "clicking")
        self.assertEqual(clicking, {"drill_id": "clicking", "label": "Precision clicking", "button": "Practice  ·  45s"})
        self.assertIn("does not mean it was applied", context["sensitivity_context"]["interpretation"])
        self.assertEqual(self.coach.calls, [])
        self.assertEqual(self.coach.reviews, [])
        # Starting another completed ordinary round refreshes report selection,
        # while ordinary announcements do not repeat the retained test review.
        self.storage.save_record(clicking_record("new-round", 1700, hits=96))
        advice = {"summary": "Your new round stayed accurate.", "cue": "Try a controlled pace increase.",
                  "drill": "clicking", "parameters": {"duration_s": 45}}
        self.storage.save_coaching("new-round", advice)
        status, _ = self.request("POST", "/voice/context", {"phase": "free", "screen": "results", "playing": False,
            "profile_settings": profile, "active_settings": profile, "coaching": advice, "speak": True})
        self.assertEqual(status, 200)
        context, speech = self.voice.updates[-2:]
        self.assertEqual(context[0]["record_id"], "new-round")
        self.assertEqual(context[0]["analysis"]["experiment_id"], experiment["id"])
        self.assertIn("controlled pace increase", speech[0])
        self.assertNotIn("inconclusive", speech[0])

    def test_voice_reload_finds_latest_completed_review_without_ordinary_rounds_or_client_analysis(self):
        self.reviewed_sensitivity("old-review", .08)
        latest = self.reviewed_sensitivity("latest-review", .07)
        self.storage.save_sensitivity_experiment({"id": "latest-pending", "status": "complete", "created_at": 1600,
                                                  "analysis": {"experiment_id": "latest-pending", "status": "inconclusive"}})
        status, _ = self.request("POST", "/voice/start", {})
        self.assertEqual(status, 200)
        context = self.voice.starts[-1]
        self.assertNotIn("report", context)
        self.assertEqual(context["analysis"]["experiment_id"], latest["id"])
        self.assertNotIn("profile_settings", context)  # A test's old base is not today's preference.
        self.assertEqual(self.coach.calls, [])
        self.assertEqual(self.coach.reviews, [])

    def test_voice_context_remains_complete_json_under_bridge_limit_with_long_coaching_memory(self):
        _, report = self.training_cycle()
        verbose = {"summary": "界" * 2000, "progress_summary": "界" * 2000, "cue": "界" * 2000,
                   "decision_reason": "界" * 2000, "success_criterion": "界" * 2000,
                   "drill": "clicking", "parameters": {"duration_s": 45},
                   "goals": [{"metric": "accuracy_pct", "operator": "at_least", "value": 98}]}
        for _ in range(4):
            self.storage.save_coaching(report["record_id"], verbose)
        experiment = self.reviewed_sensitivity()
        status, _ = self.request("POST", "/voice/context", {"profile_settings": SETTINGS, "active_settings": SETTINGS})
        self.assertEqual(status, 200)
        context = self.voice.updates[-1][0]
        encoded = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(len(encoded.encode("utf-8")), 18_000)
        self.assertEqual(json.loads(encoded)["analysis"]["experiment_id"], experiment["id"])
        self.assertEqual(context["report"]["metrics"]["accuracy_pct"]["value"], 97)
        self.assertEqual(context["comparison"]["previous_record_id"], "baseline")
        self.assertEqual(self.request("POST", "/voice/context", {"profile_settings": {"fov": float("nan")}})[0], 400)

    def test_live_voice_updates_keep_completed_evidence_without_database_reads_or_full_context_retransmission(self):
        self.training_cycle()
        experiment = self.reviewed_sensitivity()
        status, _ = self.request("POST", "/voice/context", {"phase": "free", "drill": "tracking", "playing": True,
            "profile_settings": SETTINGS, "active_settings": SETTINGS})
        self.assertEqual(status, 200)
        original_updates = len(self.voice.updates)
        payload = {"phase": "free", "drill": "tracking", "playing": True, "record_id": "retest",
                   "profile_settings": SETTINGS | {"sensitivity_deg_per_count": .06}, "active_settings": SETTINGS,
                   "live_measurements": {"elapsed_s": 5, "remaining_s": 40, "hits": 0, "shots": 0,
                       "accuracy_pct": None, "tracking_on_target_pct": 35.5,
                       "tracking_basis": "active_round_time_including_unheld_time", "snapshot_unix_s": 1705}}
        forbidden = {name: unittest.mock.Mock(side_effect=AssertionError("Live updates must not read storage"))
                     for name in ("get_report", "get_coaching", "list_sessions", "matching_history", "coaching_history",
                                  "get_sensitivity_experiment", "list_sensitivity_experiments")}
        with patch.multiple(self.storage, **forbidden):
            self.assertEqual(self.request("POST", "/voice/context", payload)[0], 200)
            payload["live_measurements"].update(elapsed_s=10, remaining_s=35, tracking_on_target_pct=40, snapshot_unix_s=1710)
            self.assertEqual(self.request("POST", "/voice/context", payload)[0], 200)
        compact = self.voice.live_updates[-1]
        self.assertEqual(len(self.voice.live_updates), 2)
        self.assertEqual(len(self.voice.updates), original_updates)
        self.assertNotIn("report", compact)
        self.assertNotIn("memory", compact)
        self.assertNotIn("analysis", compact)
        self.assertNotIn("ui_controls", compact)
        self.assertEqual(compact["live_measurements"]["tracking_on_target_pct"], 40)
        self.assertIn("ALL elapsed round time", compact["measurement_note"])
        self.assertEqual(self.server.voice_context["analysis"]["experiment_id"], experiment["id"])
        self.assertEqual(self.server.voice_context["report"]["metrics"]["accuracy_pct"]["value"], 97)
        self.assertEqual(self.server.voice_context["profile_settings"]["sensitivity_deg_per_count"], .06)
        self.assertEqual(self.coach.calls, [])
        self.assertEqual(self.coach.reviews, [])
        broken = copy.deepcopy(payload)
        broken["live_measurements"]["tracking_basis"] = "engaged_time"
        self.assertEqual(self.request("POST", "/voice/context", broken)[0], 400)
        broken = copy.deepcopy(payload)
        broken["live_measurements"]["shots"] = float("nan")
        self.assertEqual(self.request("POST", "/voice/context", broken)[0], 400)
        self.assertEqual(len(self.voice.live_updates), 2)
        self.assertEqual(self.request("POST", "/voice/context", {"phase": "paused", "drill": "tracking", "playing": False,
            "profile_settings": SETTINGS | {"sensitivity_deg_per_count": .06}, "active_settings": SETTINGS})[0], 200)
        self.assertFalse(self.server.voice_context["playing"])
        self.assertNotIn("live_measurements", self.server.voice_context)
        self.assertEqual(self.server.voice_context["analysis"]["experiment_id"], experiment["id"])

    def test_later_matched_practice_refreshes_sensitivity_evidence_and_marks_old_review_stale(self):
        experiment = self.reviewed_sensitivity()
        original = copy.deepcopy(self.storage.get_sensitivity_experiment(experiment["id"]))
        prefix = "/sensitivity/" + experiment["id"]
        status, initial = self.request("GET", prefix)
        self.assertEqual(status, 200)
        self.assertFalse(initial["review_stale"])
        current = clicking_record("later-current", 1860, hits=94, acquisition=.3)
        current["settings"] = dict(experiment["base_settings"], seed=9001)
        self.storage.save_record(current)
        self.assertFalse(self.request("GET", prefix)[1]["review_stale"])
        lower = clicking_record("later-lower", 1930, hits=97, acquisition=.25)
        lower["settings"] = dict(experiment["base_settings"], seed=9002, sensitivity_deg_per_count=.056)
        self.storage.save_record(lower)
        status, updated = self.request("GET", prefix)
        self.assertEqual(status, 200)
        self.assertTrue(updated["review_stale"])
        follow_up = updated["analysis"]["follow_up"]
        self.assertEqual(follow_up["status"], "available")
        self.assertEqual(set(follow_up["record_ids"]), {"later-current", "later-lower"})
        self.assertEqual(self.storage.get_sensitivity_experiment(experiment["id"]), original)
        status, _ = self.request("POST", "/voice/context", {"phase": "sensitivity", "playing": False,
            "analysis": {"experiment_id": experiment["id"]}, "profile_settings": lower["settings"],
            "active_settings": lower["settings"], "speak": True})
        self.assertEqual(status, 200)
        context, speech = self.voice.updates[-2:]
        self.assertTrue(context[0]["sensitivity_review_stale"])
        self.assertNotIn("sensitivity_review", context[0])
        self.assertEqual(context[0]["previous_sensitivity_review"]["reason"], original["review"]["reason"])
        self.assertEqual(set(context[0]["analysis"]["follow_up"]["record_ids"]), {"later-current", "later-lower"})
        self.assertIn("newer rounds", speech[0])
        self.assertNotIn("Keep the tested base setting", speech[0])
        self.assertEqual(self.coach.reviews, [])
        # Only the explicit review request invokes Astra, with the new evidence.
        status, pending = self.request("POST", prefix + "/review", {})
        self.assertEqual(status, 200)
        finished = wait_done(self.jobs, pending["job_id"])
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(set(self.coach.reviews[-1]["follow_up"]["record_ids"]), {"later-current", "later-lower"})
        saved = self.storage.get_sensitivity_experiment(experiment["id"])
        self.assertEqual(saved["analysis"], self.coach.reviews[-1])
        self.assertEqual(saved["record_ids"], original["record_ids"])
        self.assertEqual(saved["blocks"], original["blocks"])
        self.assertFalse(self.request("GET", prefix)[1]["review_stale"])
        # A newer run at different target/FOV conditions is not fresh comparable
        # evidence and must not invalidate the updated review.
        different = clicking_record("different-conditions", 2000, hits=99, acquisition=.15)
        different["settings"] = dict(lower["settings"], fov=90)
        self.storage.save_record(different)
        self.assertFalse(self.request("GET", prefix)[1]["review_stale"])
        self.assertEqual(len(self.coach.reviews), 1)

    def test_volume_tools_wait_for_native_ack_and_reject_stale_audio_snapshots(self):
        self.assertEqual(self.server.voice_action("set_audio_volume", {"channel": "voice", "operation": "set", "value": 40})["status"], "failed")
        self.assertEqual(self.request("POST", "/audio/state", {"voice": 100, "music": 50, "game": 70, "revision": 1.0})[0], 200)
        result, finished, worker = self.run_action("set_audio_volume", {"channel": "music", "operation": "decrease", "value": 20})
        action = self.next_action()
        self.assertEqual((action["type"], action["channel"], action["value"]), ("volume", "music", 30))
        self.assertFalse(finished.is_set())
        self.assertEqual(self.request("GET", "/health")[0], 200)  # Waiting tools do not block HTTP/audio.
        self.assertEqual(self.request("POST", "/controls/" + action["id"] + "/ack", {"status": "completed", "value": 30})[1], {"accepted": True})
        self.assertTrue(finished.wait(1))
        worker.join(1)
        self.assertGreater(result.pop("confirmed_at_unix_s"), 0)
        self.assertEqual(result, {"status": "completed", "channel": "music", "value": 30})
        self.request("POST", "/audio/state", {"voice": 100, "music": 30, "game": 70, "revision": 3})
        stale = self.request("POST", "/audio/state", {"voice": 100, "music": 50, "game": 70, "revision": 2})[1]
        self.assertFalse(stale["accepted"])
        self.assertEqual(self.server.voice_action("get_game_state", {})["state"]["audio"]["music"], 30)
        self.server.controls.timeout = .03
        timeout = self.server.voice_action("set_audio_volume", {"channel": "voice", "operation": "set", "value": 10})
        self.assertEqual(timeout["status"], "failed")
        self.assertIn("did not confirm", timeout["error"])
        self.assertEqual(self.server.audio_state["voice"], 100)
        self.assertEqual(self.request("GET", "/controls?after=" + str(action["seq"]))[1]["actions"], [])
        self.assertEqual(self.request("POST", "/audio/state", {"voice": 100, "music": float("nan"), "game": 70})[0], 400)
        self.assertEqual(self.coach.calls, [])

    def test_music_routes_and_voice_selection_never_claim_playback_before_native_confirmation(self):
        self.server.music.close()
        library = FakeMusic()
        self.server.music = library
        self.assertEqual(self.request("GET", "/music")[1]["status"], "idle")
        self.assertEqual(library.scans, [])
        self.assertEqual(self.request("POST", "/music/folder", {"path": "/selected/music"})[1]["status"], "scanning")
        self.assertEqual(library.scans, ["/selected/music"])
        self.assertEqual(self.request("GET", "/music/search?q=ambiguous")[1]["total"], 2)
        ambiguous = self.server.voice_action("music_control", {"action": "play", "query": "ambiguous"})
        self.assertEqual(ambiguous["status"], "needs_selection")
        self.assertEqual(self.request("GET", "/controls")[1]["actions"], [])
        result, finished, worker = self.run_action("music_control", {"action": "play", "track_id": "song-1"})
        action = self.next_action()
        self.assertEqual(action["track"], library.tracks[0])
        self.assertNotIn("path", action)
        self.assertGreater(action["expires_at"] - time.time(), 90)
        self.assertEqual(library.prepared, [])  # Metadata selection never transcodes.
        self.assertFalse(finished.is_set())
        prepared = self.request("POST", "/music/prepare", {"track_id": "song-1"})[1]
        self.assertEqual(prepared["format"], "wav")
        self.assertEqual(library.prepared, ["song-1"])
        self.request("POST", "/controls/" + action["id"] + "/ack", {"status": "failed", "error": "Audio decoding failed."})
        self.assertTrue(finished.wait(1))
        worker.join(1)
        self.assertEqual(result["status"], "failed")
        self.assertIn("decoding", result["error"])
        self.assertEqual(self.server.voice_action("music_control", {"action": "play", "track_id": "../song"})["status"], "failed")
        state = {"state": "playing", "track": library.tracks[0], "playing": True, "paused": False,
                 "position_s": 12.5, "volume": .5, "revision": 2.0}
        self.assertTrue(self.request("POST", "/music/state", state)[1]["accepted"])
        self.assertFalse(self.request("POST", "/music/state", state | {"revision": 1, "state": "stopped"})[1]["accepted"])
        playback = self.server.voice_action("get_game_state", {})["state"]["music"]
        self.assertEqual(playback["state"], "playing")
        self.assertEqual(playback["track"]["title"], "First song")
        self.assertNotIn("path", playback["track"])
        self.assertEqual(self.coach.calls, [])

    def test_music_mix_chooses_queue_and_reports_fresh_partial_library_and_actual_playback(self):
        self.server.music.close()
        library = FakeMusic()
        self.server.music = library
        library.scan_status = "scanning"
        before = self.server.voice_action("get_game_state", {})["state"]["music_library"]
        library.tracks.append({"id": "song-3", "title": "Newly indexed", "artist": "Third", "album": "Album"})
        after = self.server.voice_action("get_game_state", {})["state"]["music_library"]
        self.assertEqual(after["total"], before["total"] + 1)
        self.assertFalse(after["complete"])
        self.assertNotIn("root", after)
        missing = self.server.voice_action("music_control", {"action": "play", "query": "missing"})
        self.assertIn("not been indexed yet", missing["error"])
        self.assertEqual(missing["music_library"], after)
        result, finished, worker = self.run_action("music_control", {"action": "play", "mix": "jazz"})
        action = self.next_action()
        self.assertEqual(action["queue"], library.tracks)
        self.assertEqual(action["selection_label"], "Jazz music")
        self.assertFalse(finished.is_set())
        self.request("POST", "/controls/" + action["id"] + "/ack",
                     {"status": "completed", "track": library.tracks[1], "changed_track": True})
        self.assertTrue(finished.wait(1))
        worker.join(1)
        self.assertEqual(result["status"], "failed", "A different playing song must not confirm the requested one")
        self.assertIn("different song", result["error"])
        self.assertEqual(result["track"], library.tracks[1])
        result, finished, worker = self.run_action("music_control", {"action": "next"})
        action = self.next_action()
        self.request("POST", "/controls/" + action["id"] + "/ack",
                     {"status": "completed", "track": library.tracks[1], "changed_track": False})
        self.assertTrue(finished.wait(1))
        worker.join(1)
        self.assertEqual(result["status"], "failed", "Next must not confirm a switch to the same song")

    def test_personal_memory_is_separate_from_rounds_and_clear_is_bound_to_account(self):
        self.server.auth = FakeAuth(lambda: True)
        status, first = self.request("GET", "/voice/memory")
        self.assertEqual(status, 200)
        self.assertTrue(first["available"])
        self.assertEqual(first["memories"], [])
        self.assertEqual(self.server.voice_action("player_memory", {
            "action": "remember", "key": "name", "value": "Call me Morgan."})["status"], "completed")
        snapshot = self.server.voice_action("get_game_state", {})["state"]["player_memory"]
        self.assertEqual(snapshot["memories"][0]["value"], "Call me Morgan.")
        self.request("POST", "/voice/context", {"screen": "home", "playing": False})
        self.assertEqual(self.voice.updates[-1][0]["player_memory"]["count"], 1)
        with patch.object(self.server.auth, "status", return_value={"state": "signed_in", "account": {"email": "second@example.test"}}):
            second = self.request("GET", "/voice/memory")[1]
            self.assertNotEqual(first["account_generation"], second["account_generation"])
            self.assertEqual(second["memories"], [])
            self.server.voice_action("player_memory", {"action": "remember", "key": "music", "value": "I like house music."})
            self.assertEqual(self.request("POST", "/voice/memory/forget", {"all": True,
                "account_generation": first["account_generation"]})[0], 400)
            self.assertEqual(self.server.memory_snapshot()["count"], 1)
            status, cleared = self.request("POST", "/voice/memory/forget", {"all": True,
                "account_generation": second["account_generation"]})
            self.assertEqual(status, 200)
            self.assertEqual(cleared["count"], 0)
        restored = self.request("GET", "/voice/memory")[1]
        self.assertEqual(restored["memories"][0]["value"], "Call me Morgan.")
        self.assertEqual(self.coach.calls, [])
        self.assertEqual(self.storage.list_sessions(), [])

    def test_unreadable_personal_memory_does_not_break_voice_and_can_be_cleared(self):
        self.server.auth = FakeAuth(lambda: True)
        first = self.request("GET", "/voice/memory")[1]
        self.server.voice_action("player_memory", {"action": "remember", "key": "music", "value": "I like house music."})
        path = next(self.server.player_memory.directory.glob("*.json"))
        path.write_text("broken-json", encoding="utf-8")
        with self.assertLogs("companion.server", level="WARNING") as captured:
            status, snapshot = self.request("GET", "/voice/memory")
            self.assertEqual(status, 200)
            self.assertFalse(snapshot["available"])
            self.assertEqual(self.request("POST", "/voice/context", {"screen": "home", "playing": False})[0], 200)
            self.assertEqual(self.server.voice_action("get_game_state", {})["status"], "completed")
            self.assertEqual(self.server.voice_action("player_memory", {"action": "read"})["status"], "failed")
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(path.read_text(encoding="utf-8"), "broken-json")
        status, cleared = self.request("POST", "/voice/memory/forget", {"all": True,
            "account_generation": first["account_generation"]})
        self.assertEqual(status, 200)
        self.assertTrue(cleared["available"])
        self.assertEqual(cleared["count"], 0)

    def test_account_routes_quiesce_voice_and_coaching_before_scope_changes_and_reenable_only_after_login(self):
        self.training_cycle()
        auth = FakeAuth(lambda: self.voice.state == "off" and self.server.jobs.coach.closed)
        self.server.auth = auth
        new_managers = []
        def create_jobs():
            manager = JobManager(self.storage, RecordingCoach(), preconnect=False)
            new_managers.append(manager)
            return manager
        self.server._job_factory = create_jobs
        self.voice.state = "connected"
        status, pending = self.request("POST", "/account/login", {"flow": "browser"})
        self.assertEqual((status, pending["state"]), (200, "pending"))
        self.assertEqual(auth.actions[0], ("login", "browser", True))
        self.assertEqual(self.voice.state, "off")
        self.assertEqual(self.request("POST", "/voice/start", {})[0], 400)
        self.assertEqual(self.request("POST", "/coach", {"record_id": "retest"})[0], 400)
        self.assertEqual(new_managers, [])
        auth.state = "signed_in"
        self.assertEqual(self.request("GET", "/account")[1]["state"], "signed_in")
        self.assertEqual(len(new_managers), 1)
        self.assertIs(self.server.jobs, new_managers[0])
        self.assertEqual(self.request("GET", "/account")[1]["state"], "signed_in")
        self.assertEqual(len(new_managers), 1)
        self.voice.state = "connected"
        self.assertEqual(self.request("POST", "/account/logout", {})[1]["state"], "signed_out")
        self.assertEqual(auth.actions[-1], ("logout", True))
        self.assertTrue(new_managers[0].coach.closed)
        self.assertEqual(self.request("POST", "/coach", {"record_id": "retest"})[0], 400)
        self.assertEqual(self.request("GET", "/sessions")[0], 200)
        self.request("POST", "/account/login", {"flow": "device"})
        self.assertEqual(self.request("POST", "/account/login/cancel", {})[1]["state"], "signed_out")
        self.assertEqual(auth.actions[-1], ("cancel",))
        self.assertEqual(self.coach.calls, [])

    def test_health_exposes_cached_signin_errors_without_starting_account_reads(self):
        auth = FakeAuth(lambda: True)
        auth.state = "signed_out"
        self.server.auth = auth
        self.server._jobs_authorized = False
        self.server.account_status()
        with patch.object(auth, "status", side_effect=AssertionError("Health must not read account state")):
            status, health = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["account"], {"state": "signed_out", "error": None})
        unavailable = {"state": "error", "account": None, "login": None, "error": "Codex could not be launched."}
        with patch.object(auth, "status", return_value=unavailable):
            self.server.account_status()
        with patch.object(auth, "status", side_effect=AssertionError("Health must use the cache")):
            status, health = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["account"], {"state": "error", "error": "Codex could not be launched."})
        self.assertEqual(self.coach.calls, [])


if __name__ == "__main__":
    unittest.main()
