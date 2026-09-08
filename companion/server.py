"""Authenticated loopback service owned by the native game launcher."""
from __future__ import annotations

import argparse
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import logging
import math
import os
from pathlib import Path
import queue
import signal
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

from .coach import CoachCancelled, CoachError, CodexCoach, EFFORT, MODEL
from .auth import AppAuth, AuthError, shared_codex_env
from .metrics import DRILLS, analyze_record, canonicalize_json, validate_id
from .sensitivity import add_follow_up, add_result, analyze_experiment, create_experiment
from .storage import Storage
from .training import benchmark_comparison, coaching_context, contextual_history
from .voice_bridge import VoiceBridge, VoiceError

LOG = logging.getLogger(__name__)
MAX_BODY = 24 * 1024 * 1024


def sensitivity_analysis(storage, experiment):
    """Keep the measured screening immutable and attach separate later evidence."""
    analysis = analyze_experiment(experiment, [storage.get_report(identifier)
                                              for identifier in experiment["record_ids"]])
    if experiment.get("status") != "complete":
        return analysis
    rounds = [{"record": storage.get_session(summary["record_id"]),
               "report": storage.get_report(summary["record_id"])}
              for summary in storage.list_sessions(limit=30)
              if summary.get("training_context", {}).get("kind") != "sensitivity"
              and summary.get("started_at", 0) >= analysis.get("screen_completed_at", float("inf"))]
    return add_follow_up(analysis, experiment, rounds)


def sensitivity_review_stale(experiment, analysis):
    reviewed = set(experiment.get("analysis", {}).get("follow_up", {}).get("record_ids", []))
    current = set(analysis.get("follow_up", {}).get("record_ids", []))
    return bool(experiment.get("review") and current - reviewed)


class JobManager:
    def __init__(self, storage, coach=None, preconnect=True):
        self.storage = storage
        self.coach = coach or CodexCoach()
        self._jobs = {}
        self._lock = threading.RLock()
        self._queue = queue.Queue()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, args=(preconnect,), daemon=True)
        self._worker.start()

    def submit(self, record_id, question=""):
        if not isinstance(record_id, str) or not record_id or len(record_id) > 128:
            raise ValueError("A valid record_id is required.")
        if not isinstance(question, str) or len(question) > 1000:
            raise ValueError("Questions must be at most 1,000 characters.")
        report = self.storage.get_report(record_id)  # Missing record fails before spending quota.
        if report.get("training_context", {}).get("kind") == "sensitivity":
            raise ValueError("Sensitivity rounds are reviewed together after completing the test.")
        return self._submit("coaching", record_id, question)

    def submit_sensitivity(self, experiment_id):
        experiment = self.storage.get_sensitivity_experiment(experiment_id)
        if experiment.get("status") != "complete":
            raise ValueError("Complete every sensitivity block before requesting the AI review.")
        return self._submit("sensitivity", experiment_id)

    def _submit(self, kind, record_id, question=""):
        with self._lock:
            if self._stop.is_set():
                raise ValueError("Coaching is shutting down.")
            for job_id, job in self._jobs.items():
                if job["status"] == "pending" and job["kind"] == kind and job["record_id"] == record_id and job["question"] == question:
                    return job_id
            if sum(j["status"] == "pending" for j in self._jobs.values()) >= 4:
                raise ValueError("Four coaching requests are already queued. Finish or cancel one first.")
            if len(self._jobs) >= 100:
                oldest = next((key for key, job in self._jobs.items() if job["status"] != "pending"), None)
                if oldest:
                    del self._jobs[oldest]
            job_id = uuid.uuid4().hex
            self._jobs[job_id] = {"status": "pending", "kind": kind, "record_id": record_id,
                "question": question, "result": None, "error": None, "cancel": threading.Event()}
            self._queue.put(job_id)
            LOG.info("coaching_queued job_id=%s record_id=%s kind=%s", job_id, record_id, kind)
            return job_id

    def get(self, job_id):
        with self._lock:
            job = self._jobs[job_id]
            return copy.deepcopy({key: job[key] for key in ("status", "record_id", "result", "error")})

    def cancel(self, job_id):
        with self._lock:
            job = self._jobs[job_id]
            if job["status"] == "pending":
                job["cancel"].set()
                job["status"] = "cancelled"
                job["error"] = "Coaching cancelled."
                LOG.info("coaching_cancelled job_id=%s record_id=%s", job_id, job["record_id"])
            return self.get(job_id)

    def _context(self, report):
        return contextual_history(self.storage, report)

    def _run(self, preconnect):
        if preconnect:
            try:
                self.coach.connect(self._stop)
            except CoachError:
                LOG.warning("coaching_preconnect_unavailable")
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=.2)
            except queue.Empty:
                continue
            if job_id is None:
                break
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None or job["status"] != "pending":
                    continue
            try:
                if job["kind"] == "sensitivity":
                    experiment = self.storage.get_sensitivity_experiment(job["record_id"])
                    analysis = sensitivity_analysis(self.storage, experiment)
                    result = {"analysis": analysis,
                              "review": self.coach.review_sensitivity(analysis, job["cancel"]), "review_stale": False}
                else:
                    report = self.storage.get_report(job["record_id"])
                    history = self._context(report)
                    context = coaching_context(self.storage, report)
                    result = self.coach.recommend(report, history, job["question"], job["cancel"],
                        previous_coaching=self.storage.get_coaching(job["record_id"]), coaching_context=context)
                with self._lock:
                    if job["cancel"].is_set() or self._stop.is_set():
                        job["status"] = "cancelled"
                    else:
                        if job["kind"] == "sensitivity":
                            experiment.update(analysis=result["analysis"], review=result["review"], reviewed_at=time.time())
                            self.storage.save_sensitivity_experiment(experiment)
                        else:
                            # Store the decision inputs, not recursively nested copies
                            # of the journal that already stores every earlier result.
                            saved_context = {key: context[key] for key in ("training_context", "comparison")}
                            self.storage.save_coaching(job["record_id"], result, job["question"], saved_context)
                        job["result"] = result
                        job["status"] = "complete"
            except CoachCancelled:
                with self._lock:
                    job["status"] = "cancelled"
                    job["error"] = "Coaching cancelled."
            except CoachError as exc:
                with self._lock:
                    if job["status"] != "cancelled":
                        job["status"] = "failed"
                        job["error"] = str(exc)
            except Exception:
                LOG.exception("coaching_job_failed job_id=%s record_id=%s", job_id, job["record_id"])
                with self._lock:
                    if job["status"] != "cancelled":
                        job["status"] = "failed"
                        job["error"] = "Coaching could not be saved. Your practice recording is still available."
        self.coach.close()

    def close(self):
        self._stop.set()
        with self._lock:
            for job in self._jobs.values():
                if job["status"] == "pending":
                    job["cancel"].set()
                    job["status"] = "cancelled"
        self._queue.put(None)
        self._worker.join(timeout=8)
        if self._worker.is_alive():
            self.coach.close()


class NativeControls:
    """Bounded native commands; successful dispatch is not successful execution."""
    def __init__(self, timeout=10):
        self.timeout = timeout
        self._lock = threading.RLock()
        self._actions = {}
        self._seq = 0

    def dispatch(self, action, timeout=None):
        timeout = self.timeout if timeout is None else timeout
        with self._lock:
            if sum(item["result"] is None for item in self._actions.values()) >= 12:
                return {"status": "failed", "error": "Too many game controls are waiting for confirmation."}
            self._seq += 1
            identifier = uuid.uuid4().hex
            event = threading.Event()
            command = {"id": identifier, "seq": self._seq, "expires_at": time.time() + timeout, **action}
            entry = {"action": command, "event": event, "result": None}
            self._actions[identifier] = entry
            for key in list(self._actions)[:-100]:
                if self._actions[key]["result"] is not None:
                    del self._actions[key]
        event.wait(timeout)
        with self._lock:
            if entry["result"] is None:
                entry["result"] = {"status": "failed", "error": "The game did not confirm this action in time. It may not have completed."}
            return copy.deepcopy(entry["result"])

    def pending(self, after=0):
        if type(after) is not int or after < 0:
            raise ValueError("Invalid game control cursor.")
        with self._lock:
            return {"actions": [copy.deepcopy(item["action"]) for item in self._actions.values()
                    if item["action"]["seq"] > after and item["result"] is None
                    and item["action"]["expires_at"] > time.time()], "last_seq": self._seq}

    def acknowledge(self, identifier, result):
        validate_id(identifier)
        if result.get("status") not in {"completed", "failed"}:
            raise ValueError("Game actions must be acknowledged as completed or failed.")
        response = {"status": result["status"]}
        if "value" in result:
            number = result["value"]
            if type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 200:
                raise ValueError("Invalid applied audio level.")
            response["value"] = number
        for key in ("message", "error"):
            if key in result:
                if not isinstance(result[key], str) or len(result[key]) > 500:
                    raise ValueError("Invalid game action acknowledgement.")
                response[key] = result[key]
        with self._lock:
            entry = self._actions[identifier]
            if entry["result"] is None and entry["action"]["expires_at"] > time.time():
                entry["result"] = response
                entry["event"].set()
                return {"accepted": True}
            return {"accepted": False}

    def cancel_all(self, reason):
        with self._lock:
            for item in self._actions.values():
                if item["result"] is None:
                    item["result"] = {"status": "failed", "error": reason}
                    item["event"].set()


class CompanionServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, token, storage, jobs, voice=None, music=None, auth=None, job_factory=None):
        self.token = token
        self.storage = storage
        self.jobs = jobs
        self.workflow_lock = threading.RLock()
        self.volume_lock = threading.Lock()
        self.account_lock = threading.RLock()
        self.voice_context = {}
        self.audio_state = None
        self.audio_revision = None
        self.music_state = None
        self.controls = NativeControls()
        self._account_transition = False
        self._jobs_authorized = auth is None
        self._closing = False
        self._account_snapshot = {"state": "checking" if auth is not None else "unavailable", "error": None}
        self.auth = auth
        self.env_factory = (lambda: shared_codex_env(storage.data_dir)) if hasattr(storage, "data_dir") else os.environ.copy
        self._job_factory = job_factory or (lambda: JobManager(storage, coach=CodexCoach(env_factory=self.env_factory)))
        if music is None and hasattr(storage, "data_dir"):
            from .music import MusicLibrary
            music = MusicLibrary(storage.data_dir)
        self.music = music
        self.voice = voice if voice is not None else VoiceBridge(action_handler=self.voice_action, env_factory=self.env_factory)
        super().__init__(address, Handler)

    def server_close(self):
        with self.account_lock:
            self._closing = True
            self.controls.cancel_all("The game is closing.")
            self.voice.close()
            self.jobs.close()
            if self.music is not None:
                self.music.close()
            if self.auth is not None:
                self.auth.close()
        super().server_close()

    def _cache_account(self, state):
        with self.workflow_lock:
            self._account_snapshot = {"state": state["state"], "error": state.get("error")}
        return state

    def account_status(self):
        if self.auth is None:
            return {"state": "unavailable", "account": None, "login": None, "error": "Account controls are unavailable."}
        with self.account_lock:
            if self._closing:
                return {"state": "unavailable", "account": None, "login": None, "error": "The game is closing."}
            state = self.auth.status()
            self._cache_account(state)
            if state["state"] == "signed_in" and not self._jobs_authorized:
                self.jobs.close()
                self.jobs = self._job_factory()
                self._jobs_authorized = True
                self._account_transition = False
            elif state["state"] != "signed_in" and self._jobs_authorized:
                self._quiesce_account()
            return state

    def _quiesce_account(self):
        self._account_transition = True
        self.controls.cancel_all("Account access changed before the game confirmed this action.")
        self.voice.stop()
        self.jobs.close()
        self._jobs_authorized = False

    def account_action(self, action, flow="browser"):
        if self.auth is None:
            raise ValueError("Account controls are unavailable.")
        if action == "login" and (not isinstance(flow, str) or flow not in {"browser", "device"}):
            raise ValueError("Choose browser or device sign-in.")
        with self.account_lock:
            if self._closing:
                raise ValueError("The game is closing.")
            if action in {"login", "logout"}:
                self._quiesce_account()
            if action == "login":
                return self._cache_account(self.auth.start_login(flow=flow))
            if action == "cancel":
                state = self.auth.cancel_login()
            elif action == "logout":
                state = self.auth.logout()
            else:
                raise ValueError("Unknown account action.")
            self._account_transition = False
            return self._cache_account(state)

    def require_account(self):
        if self.auth is not None and self.account_status()["state"] != "signed_in":
            raise ValueError("Sign in with ChatGPT before starting coaching or Juniper.")

    def set_audio_state(self, value):
        value = canonicalize_json(value)
        if not {"voice", "music", "game"} <= value.keys() or set(value) - {"voice", "music", "game", "revision"}:
            raise ValueError("Audio state requires voice, music and game levels.")
        for number in (value[key] for key in ("voice", "music", "game")):
            if type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 200:
                raise ValueError("Audio levels must be percentages between 0 and 200.")
        revision = value.get("revision")
        if revision is not None and (type(revision) is not int or revision < 0):
            raise ValueError("Audio revision must be a nonnegative integer.")
        levels = {key: value[key] for key in ("voice", "music", "game")}
        with self.workflow_lock:
            if revision is not None and self.audio_revision is not None and revision <= self.audio_revision:
                return {"accepted": False, "audio": copy.deepcopy(self.audio_state)}
            self.audio_state = levels
            if revision is not None:
                self.audio_revision = revision
        return {"accepted": True, "audio": copy.deepcopy(levels)}

    def set_music_state(self, value):
        value = canonicalize_json(value)
        expected = {"state", "track", "playing", "paused", "position_s", "volume", "revision"}
        if set(value) != expected or not isinstance(value["state"], str) or value["state"] not in {"playing", "paused", "stopped"}:
            raise ValueError("Invalid native music state.")
        if type(value["revision"]) is not int or value["revision"] < 0:
            raise ValueError("Music state revision must be a nonnegative integer.")
        if not isinstance(value["playing"], bool) or not isinstance(value["paused"], bool):
            raise ValueError("Music playback flags must be boolean.")
        for key, maximum in (("position_s", 1e7), ("volume", 2)):
            if type(value[key]) not in (int, float) or not math.isfinite(value[key]) or not 0 <= value[key] <= maximum:
                raise ValueError("Invalid native music timing or volume.")
        if not isinstance(value["track"], dict) or set(value["track"]) - {"id", "title", "artist", "album"}:
            raise ValueError("Music state must contain only public track metadata.")
        for key, text in value["track"].items():
            if not isinstance(text, str) or len(text) > 500:
                raise ValueError("Invalid music track metadata.")
            if key == "id" and text:
                validate_id(text)
        with self.workflow_lock:
            if self.music_state is not None and value["revision"] <= self.music_state["revision"]:
                return {"accepted": False}
            self.music_state = copy.deepcopy(value)
        return {"accepted": True}

    def voice_action(self, name, arguments):
        """Called on a worker thread by the backing voice model's bounded tools."""
        try:
            if self._account_transition:
                return {"status": "failed", "error": "Account access is changing. Try again after sign-in."}
            if not isinstance(arguments, dict):
                raise ValueError("Game tool arguments must be an object.")
            if name == "get_game_state":
                if arguments:
                    raise ValueError("get_game_state takes no arguments.")
                with self.workflow_lock:
                    state = {key: copy.deepcopy(self.voice_context[key]) for key in
                             ("phase", "screen", "drill", "drill_label", "playing", "profile_settings", "active_settings", "live_measurements", "ui_controls")
                             if key in self.voice_context}
                    state["audio"] = copy.deepcopy(self.audio_state)
                    state["music"] = copy.deepcopy(self.music_state)
                return {"status": "completed", "state": state}
            if name == "set_audio_volume":
                if set(arguments) != {"channel", "operation", "value"}:
                    raise ValueError("Volume requires channel, operation and value.")
                channel, operation, amount = arguments["channel"], arguments["operation"], arguments["value"]
                if (not isinstance(channel, str) or not isinstance(operation, str)
                        or channel not in {"voice", "music", "game"} or operation not in {"set", "increase", "decrease"}):
                    raise ValueError("Unknown audio channel or volume operation.")
                if type(amount) not in (int, float) or not math.isfinite(amount) or not 0 <= amount <= 200:
                    raise ValueError("Volume values must be percentages between 0 and 200.")
                with self.volume_lock:
                    with self.workflow_lock:
                        if self.audio_state is None:
                            raise ValueError("The game has not supplied its current volume levels yet.")
                        before = self.audio_state[channel]
                    requested = amount if operation == "set" else min(200, max(0, before + amount * (1 if operation == "increase" else -1)))
                    result = self.controls.dispatch({"type": "volume", "channel": channel, "value": requested})
                    if result["status"] == "completed" or "value" in result:
                        applied = result.get("value", requested)
                        with self.workflow_lock:
                            self.audio_state[channel] = applied
                        result.update(channel=channel, value=applied)
                    return result
            if name == "music_control":
                if self.music is None:
                    raise ValueError("The music library is unavailable.")
                if set(arguments) - {"action", "query", "track_id"}:
                    raise ValueError("Unsupported music arguments.")
                action = arguments.get("action")
                if not isinstance(action, str) or action not in {"play", "pause", "resume", "stop", "next"}:
                    raise ValueError("Unknown music action.")
                command = {"type": "music", "action": action}
                if action == "play":
                    if bool(arguments.get("query")) == bool(arguments.get("track_id")):
                        raise ValueError("Play requires one song query or indexed track id.")
                    if arguments.get("track_id"):
                        identifier = validate_id(arguments["track_id"])
                        # Lookup by opaque indexed id; only the native game receives
                        # the prepared local audio path through its existing endpoint.
                        track = self.music.get_track(identifier)
                    else:
                        query = arguments["query"]
                        if not isinstance(query, str) or len(query) > 200:
                            raise ValueError("Song queries must be at most 200 characters.")
                        matches = self.music.search(query, limit=10)
                        tracks = matches.get("tracks", [])
                        exact = matches.get("exact_match_id")
                        track = next((item for item in tracks if item["id"] == exact), None)
                        if track is None and len(tracks) == 1 and not matches.get("ambiguous"):
                            track = tracks[0]
                        if track is None:
                            if not tracks:
                                return {"status": "failed", "error": "No indexed song matched that request."}
                            return {"status": "needs_selection", "message": "More than one indexed song matches. Ask which one to play.",
                                    "tracks": [{key: item.get(key, "") for key in ("id", "title", "artist", "album")} for item in tracks]}
                    command["track"] = {key: track.get(key, "") for key in ("id", "title", "artist", "album")}
                elif arguments.get("query") or arguments.get("track_id"):
                    raise ValueError("Only play accepts a song selection.")
                return self.controls.dispatch(command, timeout=95 if action in {"play", "next"} else None)
            raise ValueError("That game tool is not supported.")
        except (ValueError, KeyError) as exc:
            return {"status": "failed", "error": str(exc)[:300]}

    def sensitivity_state(self, experiment_id):
        experiment = self.storage.get_sensitivity_experiment(experiment_id)
        analysis = sensitivity_analysis(self.storage, experiment)
        return {"experiment": experiment, "analysis": analysis,
                "review_stale": sensitivity_review_stale(experiment, analysis)}

    def update_voice_context(self, value):
        """UI state is bounded; measurements and advice always come from storage."""
        context = {}
        for name in ("phase", "screen", "drill", "cue"):
            if name in value:
                if not isinstance(value[name], str) or len(value[name]) > (500 if name == "cue" else 80):
                    raise ValueError(f"Invalid voice context {name}.")
                context[name] = value[name]
        if context.get("drill") and context["drill"] not in DRILLS:
            raise ValueError("Unknown voice context drill.")
        for name in ("playing", "speak"):
            if name in value and not isinstance(value[name], bool):
                raise ValueError(f"Voice context {name} must be boolean.")
        context["playing"] = value.get("playing", False)
        with self.workflow_lock:
            previous_profile = copy.deepcopy(self.voice_context.get("profile_settings"))
        for name in ("active_settings", "profile_settings"):
            if name in value:
                context[name] = self._voice_settings(value[name], name)
        if "profile_settings" not in context and previous_profile is not None:
            context["profile_settings"] = previous_profile
        if context.get("drill"):
            context["drill_label"] = ("Reactive tracking" if context["drill"] == "tracking" and
                context.get("active_settings", {}).get("tracking_motion") == "reactive" else
                {"clicking": "Precision clicking", "tracking": "Smooth tracking", "switching": "Target switching"}[context["drill"]])
        if "live_measurements" in value:
            if value.get("speak"):
                raise ValueError("Live measurements update context silently.")
            if not context["playing"]:
                raise ValueError("Live measurements require an active round.")
            context["live_measurements"] = self._voice_live_measurements(value["live_measurements"])
            context["measurement_note"] = "Provisional telemetry from the current active round, separate from completed evidence. Tracking percentage uses ALL elapsed round time, including time fire was not held; it is not the final engaged-time tracking metric. The coach has aim measurements, not screen video."
            with self.workflow_lock:
                merged = copy.deepcopy(self.voice_context)
                merged.update(context)
                self._fit_voice_context(merged)
                self.voice_context = merged
            return self.voice.update_live_state(context)
        context["ui_controls"] = self._voice_controls(context.get("profile_settings", {}))
        record_id = value.get("record_id")
        if not record_id:
            latest = next((item for item in self.storage.list_sessions(limit=100)
                           if item.get("training_context", {}).get("kind") != "sensitivity"), None)
            record_id = latest["record_id"] if latest else None
        if record_id:
            report = self.storage.get_report(validate_id(record_id))
            voice_report = {key: report[key] for key in ("record_id", "drill", "started_at", "quality", "benchmark_key")}
            voice_report["metrics"] = {name: {key: metric[key] for key in ("value", "unit")}
                                       for name, metric in report.get("metrics", {}).items()}
            voice_report["evidence"] = [{"id": evidence["id"], "summary": evidence.get("summary", "")}
                                        for evidence in report.get("evidence", [])]
            context.update(record_id=record_id, report=voice_report,
                           comparison=benchmark_comparison(self.storage, report),
                           coaching=self._voice_advice(self.storage.get_coaching(record_id)))
            memory = coaching_context(self.storage, report)
            memory["journal"] = [{"record_id": entry["record_id"], "drill": entry["drill"],
                                   "result": self._voice_advice(entry["result"])} for entry in memory["journal"][:3]]
            memory.pop("comparison", None)  # The identical authoritative comparison is already above.
            context["memory"] = memory
            context["round_context"] = "The report describes a completed ordinary round, separate from sensitivity screening. Drill and active_settings describe the current activity, not new measurements."
        requested_sensitivity = "sensitivity_review" in value or "analysis" in value
        if requested_sensitivity:
            analysis = value.get("analysis")
            if not isinstance(analysis, dict):
                raise ValueError("Sensitivity voice context requires an experiment analysis.")
            experiment = self.storage.get_sensitivity_experiment(validate_id(analysis.get("experiment_id")))
            if not self._reviewed_experiment(experiment):
                raise ValueError("The sensitivity review is not ready yet.")
        else:
            # Reload the newest completed review, even after navigation or a
            # companion restart. A newer unfinished screening must not hide it.
            experiment = next((item for item in self.storage.list_sensitivity_experiments(limit=100)
                               if self._reviewed_experiment(item)), None)
        if experiment:
            stored = sensitivity_analysis(self.storage, experiment)
            review_stale = sensitivity_review_stale(experiment, stored)
            compact = {key: stored[key] for key in ("experiment_id", "status", "confidence", "allowed_candidate_ids",
                                                    "strong_candidate_ids", "recommended_candidate_id", "explanation") if key in stored}
            compact["candidates"] = [{key: candidate[key] for key in ("id", "sensitivity_deg_per_count", "metrics",
                "repeat_consistency", "quality_flags", "regression_flags") if key in candidate} for candidate in stored.get("candidates", [])]
            review = {key: str(experiment["review"][key])[:700] for key in
                      ("summary", "reason", "recommended_candidate_id", "confidence", "next_step") if key in experiment["review"]}
            if stored.get("follow_up"):
                compact["follow_up"] = copy.deepcopy(stored["follow_up"])
                compact["follow_up"].pop("evidence", None)  # Comparisons retain values and source ids.
            context.update(analysis=compact, sensitivity_review_stale=review_stale,
                           sensitivity_context={"experiment_id": experiment["id"], "reviewed_at": experiment.get("reviewed_at"),
                               "tested_base_settings": self._voice_settings(experiment.get("base_settings", {}), "tested_base_settings"),
                               "interpretation": "Candidate 'current' means the test's original base sensitivity. profile_settings are the player's saved preferences; active_settings are the temporary current drill settings. A recommendation does not mean it was applied. Use this analysis and review for sensitivity questions, not the separate ordinary-round report."})
            if review_stale:
                context["previous_sensitivity_review"] = review
                context["sensitivity_context"]["interpretation"] += " The previous review did not include the newer follow_up results. Do not present its recommendation as current; discuss the measured later changes as observational evidence, not causal proof."
            else:
                context["sensitivity_review"] = review
        announce_sensitivity = bool(experiment and
                                    (requested_sensitivity or context.get("phase") == "sensitivity"))
        if value.get("speak") and not (announce_sensitivity or context.get("coaching")):
            raise ValueError("Recorded coaching is required before announcing advice.")
        self._fit_voice_context(context)
        with self.workflow_lock:
            self.voice_context = context
        status = self.voice.update_context(context, speak=False)
        if value.get("speak"):
            if announce_sensitivity:
                if context.get("sensitivity_review_stale"):
                    review = {"summary": "New practice results were recorded after that review.",
                              "reason": context["analysis"].get("follow_up", {}).get("summary", "The sensitivity comparison has new evidence."),
                              "next_step": "Request an updated sensitivity review before treating the old recommendation as current."}
                else:
                    review = context["sensitivity_review"]
                # Reserve words for the concrete finding, rather than spending
                # the entire announcement repeating a generic headline.
                speech = " ".join(" ".join(str(review.get(key, "")).split()[:limit])
                                  for key, limit in (("summary", 18), ("reason", 42), ("next_step", 20)))
            else:
                advice = context["coaching"]
                duration = advice.get("parameters", {}).get("duration_s", 45)
                names = {"clicking": "Precision clicking", "tracking": "Smooth tracking", "switching": "Target switching"}
                speech = " ".join((advice.get("progress_summary", advice.get("summary", "")), advice.get("cue", ""),
                                   f"Next, {duration} seconds of {names.get(advice.get('drill'), 'practice')}."))
            # appendSpeech is literal speech, never a prompt or a JSON payload.
            status = self.voice.update_context(" ".join(speech.split()[:80]), speak=True)
        return status

    @staticmethod
    def _voice_settings(settings, label):
        if not isinstance(settings, dict) or len(settings) > 64:
            raise ValueError(f"Voice {label} must be an object.")
        selected = {}
        for name, low, high in (("sensitivity_deg_per_count", .00001, 10), ("fov", 10, 170),
                                ("duration_s", 0, 3600), ("target_scale", .05, 20),
                                ("speed_scale", .05, 20), ("dpi", 50, 64000)):
            if name in settings:
                number = settings[name]
                if type(number) not in (int, float) or not math.isfinite(number) or not low <= number <= high:
                    raise ValueError(f"Invalid voice {label} {name}.")
                selected[name] = number
        if "tracking_motion" in settings:
            if not isinstance(settings["tracking_motion"], str) or settings["tracking_motion"] not in {"smooth", "reactive"}:
                raise ValueError("Unknown tracking motion.")
            selected["tracking_motion"] = settings["tracking_motion"]
        return selected

    @staticmethod
    def _voice_controls(profile):
        duration = profile.get("duration_s")
        button = f"Practice  ·  {int(duration)}s" if duration is not None else "Practice (the button also shows your round duration)"
        return {"training_tab": "Train", "drill_cards": [
                    {"drill_id": "clicking", "label": "Precision clicking", "button": button},
                    {"drill_id": "tracking", "label": "Smooth tracking", "button": button},
                    {"drill_id": "tracking", "tracking_motion": "reactive", "label": "Reactive tracking", "button": button},
                    {"drill_id": "switching", "label": "Target switching", "button": button}],
                "home_buttons": ["Start guided baseline", "Find my sensitivity"],
                "pause": {"key": "Escape", "buttons": ["Resume round", "End round and return"]},
                "instruction": "Use these exact visible names when directing the player. Voice cannot activate controls or change settings."}

    @staticmethod
    def _voice_live_measurements(measurements):
        if not isinstance(measurements, dict):
            raise ValueError("Live measurements must be an object.")
        bounds = {"elapsed_s": (0, 3600), "remaining_s": (0, 3600), "hits": (0, 20000), "shots": (0, 20000),
                  "accuracy_pct": (0, 100), "tracking_on_target_pct": (0, 100), "snapshot_unix_s": (0, 1e12)}
        if set(measurements) - (bounds.keys() | {"tracking_basis"}):
            raise ValueError("Unsupported live measurement.")
        selected = {}
        for name, number in measurements.items():
            if name == "tracking_basis":
                if number != "active_round_time_including_unheld_time":
                    raise ValueError("Unsupported live tracking measurement basis.")
                selected[name] = number
                continue
            if number is None and name in {"accuracy_pct", "tracking_on_target_pct"}:
                selected[name] = None
                continue
            low, high = bounds[name]
            if type(number) not in (int, float) or not math.isfinite(number) or not low <= number <= high:
                raise ValueError(f"Invalid live measurement {name}.")
            if name in {"hits", "shots"} and int(number) != number:
                raise ValueError(f"Live measurement {name} must be an integer.")
            selected[name] = number
        if not {"elapsed_s", "remaining_s", "hits", "shots", "snapshot_unix_s", "tracking_basis"} <= selected.keys():
            raise ValueError("Live measurements require timing, hits, shots and tracking basis.")
        if selected["hits"] > selected["shots"]:
            raise ValueError("Live hits cannot exceed shots.")
        return selected

    @staticmethod
    def _reviewed_experiment(experiment):
        return (experiment.get("status") == "complete" and isinstance(experiment.get("review"), dict)
                and bool(experiment["review"]) and isinstance(experiment.get("analysis"), dict)
                and experiment["analysis"].get("status") in {"inconclusive", "provisional"}
                and experiment["analysis"].get("experiment_id") == experiment.get("id"))

    @staticmethod
    def _fit_voice_context(context):
        """Keep complete JSON below the bridge's 18k-character context ceiling."""
        def size():
            return len(json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        journal = context.get("memory", {}).get("journal", [])
        while journal and size() > 17_000:
            journal.pop()
        if size() > 17_000:
            prose = {"summary", "cue", "progress_summary", "decision_reason", "success_criterion",
                     "reason", "next_step", "explanation"}
            def shorten(item):
                if isinstance(item, dict):
                    for key, value in item.items():
                        if key in prose and isinstance(value, str):
                            item[key] = value[:200]
                        else:
                            shorten(value)
                elif isinstance(item, list):
                    for value in item:
                        shorten(value)
            shorten(context)
        if size() > 17_000:
            context.get("report", {}).pop("evidence", None)
        if size() > 17_000:
            raise ValueError("The voice context exceeds its supported size.")

    @staticmethod
    def _voice_advice(advice):
        if not isinstance(advice, dict):
            return None
        selected = {key: copy.deepcopy(advice[key]) for key in ("summary", "cue", "progress_summary", "decision", "decision_reason",
            "goal_status", "goals", "drill", "parameters", "success_criterion") if key in advice}
        return {key: value[:500] if isinstance(value, str) else value for key, value in selected.items()}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, *_):
        pass  # Do not log headers, prompts, or recordings.

    def _send(self, code, value):
        payload = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self):
        expected = "Bearer " + self.server.token
        actual = self.headers.get("Authorization", "")
        if self.headers.get("Origin") or not hmac.compare_digest(actual.encode(), expected.encode()):
            self.close_connection = True
            self._send(401, {"error": "Unauthorized."})
            return False
        return True

    def _json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid request size.") from exc
        if length <= 0 or length > MAX_BODY:
            self.close_connection = True
            raise ValueError("Request must contain JSON and be smaller than 24 MiB.")
        if self.headers.get_content_type() != "application/json":
            self.close_connection = True
            raise ValueError("Content-Type must be application/json.")
        try:
            value = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("Invalid JSON.") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON must be an object.")
        return value

    def do_GET(self):
        self._handle(False)

    def do_POST(self):
        self._handle(True)

    def _handle(self, post):
        if not self._authorized():
            return
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/")
        parts = path.strip("/").split("/")
        try:
            if not post and path == "/health":
                coach = self.server.jobs.coach
                with self.server.workflow_lock:
                    account = copy.deepcopy(self.server._account_snapshot)
                result = {"status": "ok", "model": MODEL, "effort": EFFORT,
                    "connection": coach.state, "error": coach.last_error, "account": account}
            elif not post and path == "/sessions":
                result = {"sessions": self.server.storage.list_sessions()}
            elif not post and len(parts) == 2 and parts[0] == "sessions":
                record_id = parts[1]
                result = {"record": self.server.storage.get_session(record_id),
                    "report": self.server.storage.get_report(record_id),
                    "coaching": self.server.storage.get_coaching(record_id)}
                result["comparison"] = benchmark_comparison(self.server.storage, result["report"])
            elif post and path == "/sessions":
                report = self.server.storage.save_record(self._json())
                result = {"report": report, "comparison": benchmark_comparison(self.server.storage, report)}
            elif post and path == "/coach":
                value = self._json()
                self.server.require_account()
                result = {"job_id": self.server.jobs.submit(value.get("record_id"), value.get("question", ""))}
            elif post and path == "/sensitivity/start":
                value = self._json()
                experiment = create_experiment(value.get("settings"))
                self.server.storage.save_sensitivity_experiment(experiment)
                result = {"experiment": experiment}
            elif not post and path == "/sensitivity":
                result = {"experiments": self.server.storage.list_sensitivity_experiments()}
            elif not post and len(parts) == 2 and parts[0] == "sensitivity":
                result = self.server.sensitivity_state(parts[1])
            elif post and len(parts) == 3 and parts[0] == "sensitivity" and parts[2] == "round":
                value = self._json()
                record = canonicalize_json(value.get("record"))
                report = analyze_record(record)
                if value.get("block_index") != record.get("sensitivity_context", {}).get("block_index"):
                    raise ValueError("Sensitivity block index does not match the round.")
                with self.server.workflow_lock:
                    experiment = self.server.storage.get_sensitivity_experiment(parts[1])
                    updated = add_result(experiment, record, report)
                    self.server.storage.save_record(record)
                    self.server.storage.save_sensitivity_experiment(updated)
                    result = self.server.sensitivity_state(parts[1])
            elif post and len(parts) == 3 and parts[0] == "sensitivity" and parts[2] == "review":
                self._json()
                self.server.require_account()
                result = {"job_id": self.server.jobs.submit_sensitivity(parts[1])}
            elif not post and path == "/account":
                result = self.server.account_status()
            elif post and path == "/account/login":
                value = self._json()
                result = self.server.account_action("login", value.get("flow", "browser"))
            elif post and path == "/account/login/cancel":
                self._json()
                result = self.server.account_action("cancel")
            elif post and path == "/account/logout":
                self._json()
                result = self.server.account_action("logout")
            elif not post and path == "/music":
                result = self.server.music.status()
            elif post and path == "/music/folder":
                value = self._json()
                result = self.server.music.start_scan(value.get("path"))
            elif not post and path == "/music/search":
                query = parse_qs(parsed.query).get("q", [""])
                if len(query) != 1 or len(query[0]) > 200:
                    raise ValueError("Music searches must be at most 200 characters.")
                result = self.server.music.search(query[0], limit=50)
            elif post and path == "/music/prepare":
                value = self._json()
                result = self.server.music.prepare(validate_id(value.get("track_id")))
            elif post and path == "/audio/state":
                result = self.server.set_audio_state(self._json())
            elif post and path == "/music/state":
                result = self.server.set_music_state(self._json())
            elif not post and path == "/controls":
                after = parse_qs(parsed.query).get("after", ["0"])
                if len(after) != 1 or not after[0].isdigit() or len(after[0]) > 16:
                    raise ValueError("Invalid game control cursor.")
                result = self.server.controls.pending(after=int(after[0]))
            elif post and len(parts) == 3 and parts[0] == "controls" and parts[2] == "ack":
                result = self.server.controls.acknowledge(parts[1], self._json())
            elif not post and path == "/voice/status":
                result = self.server.voice.status()
            elif post and path == "/voice/start":
                self._json()
                self.server.require_account()
                with self.server.workflow_lock:
                    context = copy.deepcopy(self.server.voice_context)
                if not context:
                    self.server.update_voice_context({"playing": False})
                    with self.server.workflow_lock:
                        context = copy.deepcopy(self.server.voice_context)
                result = self.server.voice.start(context=context)
            elif post and path == "/voice/stop":
                self._json()
                result = self.server.voice.stop()
            elif post and path == "/voice/audio":
                value = self._json()
                result = self.server.voice.push_audio(value.get("audio"),
                    sample_rate=value.get("sample_rate", 24000), num_channels=value.get("num_channels", 1))
            elif not post and path == "/voice/events":
                after = parse_qs(parsed.query).get("after", ["0"])
                if len(after) != 1 or not after[0].isdigit() or len(after[0]) > 16:
                    raise ValueError("Invalid voice event cursor.")
                result = self.server.voice.events(after=int(after[0]))
            elif post and path == "/voice/context":
                result = self.server.update_voice_context(self._json())
            elif not post and len(parts) == 2 and parts[0] == "jobs":
                result = self.server.jobs.get(parts[1])
            elif post and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
                self._json()  # Consume the JSON body for keep-alive connections.
                result = self.server.jobs.cancel(parts[1])
            else:
                self.close_connection = True
                self._send(404, {"error": "Not found."})
                return
            self._send(200, result)
        except KeyError:
            self._send(404, {"error": "That session or coaching request was not found."})
        except ValueError as exc:
            self._send(400, {"error": str(exc)[:300]})
        except VoiceError as exc:
            self._send(400, {"error": str(exc)[:300]})
        except AuthError as exc:
            self._send(400, {"error": str(exc)[:300]})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            LOG.exception("http_request_failed method=%s route=%s", self.command, parts[0])
            self._send(500, {"error": "The companion could not complete this request."})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    token = os.environ.get("AIMCOACH_TOKEN", "")
    if len(token) < 24:
        parser.error("AIMCOACH_TOKEN must contain an ephemeral token of at least 24 characters.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    storage = Storage(args.data_dir)
    auth = AppAuth(args.data_dir)
    jobs = JobManager(storage, coach=CodexCoach(env_factory=lambda: shared_codex_env(args.data_dir)), preconnect=False)
    server = CompanionServer(("127.0.0.1", args.port), token, storage, jobs, auth=auth)

    def stop(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(json.dumps({"port": server.server_address[1]}), flush=True)
    threading.Thread(target=server.account_status, daemon=True, name="shadow-aim-account-status").start()
    try:
        server.serve_forever(poll_interval=.2)
    finally:
        server.server_close()
        server.jobs.close()
        close_storage = getattr(storage, "close", None)
        if close_storage:
            close_storage()


if __name__ == "__main__":
    main()
