"""Real companion HTTP -> native player -> acknowledgement, without an account."""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from urllib.parse import quote
from urllib.request import Request, urlopen
import wave

from companion.server import CompanionServer, NativeControls
from companion.storage import Storage
from companion.music import MusicError, _av

ROOT = Path(__file__).resolve().parents[1]


def native_runtime():
    filename = "godot.exe" if os.name == "nt" else "godot"
    platform = "windows-x86_64" if os.name == "nt" else "linux-x86_64"
    candidates = [os.environ.get("GODOT_BIN"), ROOT / "runtime" / filename,
                  ROOT / "build/runtime-cache" / platform / filename,
                  "/snap/godot-4/current/godot-4", shutil.which("godot-4"),
                  shutil.which("godot4"), shutil.which("godot")]
    return next((str(Path(path).resolve()) for path in candidates if path and Path(path).is_file()), None)


class OfflineVoice:
    def status(self):
        return {"state": "off", "error": None}

    def update_context(self, *_args, **_kwargs):
        return self.status()

    update_live_state = update_context

    def close(self):
        pass


class OfflineJobs:
    coach = SimpleNamespace(state="connecting", last_error=None)

    def close(self):
        pass


class BoundedControls(NativeControls):
    def dispatch(self, action, timeout=None):
        return super().dispatch(action, timeout=min(timeout or self.timeout, 5.0))


class NativeMusicHTTPTests(unittest.TestCase):
    def setUp(self):
        self.godot = native_runtime()
        if not self.godot:
            self.skipTest("Godot 4 is required for native HTTP integration")
        try:
            _av()
        except MusicError:
            self.skipTest("PyAV is required for native HTTP integration")
        self.temp = tempfile.TemporaryDirectory(prefix="shadow-native-music-http-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.token = secrets.token_urlsafe(24)
        self.process = None
        self.server = None
        self.thread = None
        self.output = None
        self.addCleanup(self.close_server)
        self.addCleanup(self.close_native)
        self.environment = {key: value for key, value in os.environ.items()
                            if key not in {"OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"}}
        for key, subdir in (("CODEX_HOME", "empty-codex"), ("XDG_DATA_HOME", "xdg"),
                            ("APPDATA", "roaming"), ("LOCALAPPDATA", "local")):
            directory = self.base / subdir
            directory.mkdir()
            self.environment[key] = str(directory)
        if self.godot.startswith("/snap/godot-4/"):
            self.environment["DOTNET_ROOT"] = "/snap/godot-4/current/usr/lib/dotnet"
            self.environment["PATH"] = self.environment["DOTNET_ROOT"] + os.pathsep + self.environment.get("PATH", "")
        self.stop_path = self.base / "stop"
        self.ready_path = self.base / "ready.json"
        self.environment.update(AIMCOACH_TOKEN=self.token, AIMCOACH_DATA_DIR=str(self.base / "data"),
                                AIMCOACH_TEST_STOP=str(self.stop_path), AIMCOACH_TEST_READY=str(self.ready_path))
        self.start_server()

    def start_server(self):
        self.server = CompanionServer(("127.0.0.1", 0), self.token, Storage(self.base / "data"),
                                      OfflineJobs(), voice=OfflineVoice())
        self.server.controls = BoundedControls(timeout=3.0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.environment["AIMCOACH_PORT"] = str(self.server.server_address[1])

    def close_server(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=3)
            self.server = None

    def start_native(self):
        self.stop_path.unlink(missing_ok=True)
        self.ready_path.unlink(missing_ok=True)
        self.output = (self.base / "native.log").open("w", encoding="utf-8")
        self.process = subprocess.Popen([self.godot, "--path", str(ROOT), "--headless", "--audio-driver", "Dummy",
                                         "--script", "res://tests/native_music_http_smoke.gd"], cwd=ROOT,
                                        env=self.environment, stdout=self.output, stderr=subprocess.STDOUT)
        self.eventually(lambda: self.ready_path.is_file() and self.server.audio_state is not None)
        self.settings_path = Path(json.loads(self.ready_path.read_text())["settings_path"])
        self.assertTrue(self.settings_path.resolve().is_relative_to(self.base.resolve()), "Settings must remain inside the test fixture")

    def close_native(self):
        if self.process:
            self.stop_path.touch()
            try:
                self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            self.process = None
        if self.output:
            self.output.close()
            self.output = None

    def eventually(self, predicate, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            if self.process and self.process.poll() is not None:
                break
            time.sleep(.02)
        log = (self.base / "native.log").read_text(errors="replace") if (self.base / "native.log").exists() else ""
        self.fail("Native state did not arrive:\n" + log[-4000:])

    def http(self, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(f"http://127.0.0.1:{self.server.server_address[1]}{path}", data=data,
                          headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with urlopen(request, timeout=6) as response:
            return json.load(response)

    def music_action(self, action, **selection):
        result = self.server.voice_action("music_control", {"action": action, **selection})
        self.assertEqual(result["status"], "completed", result)
        return result

    def music_state(self, state, identifier=None):
        self.eventually(lambda: self.server.music_state is not None and self.server.music_state["state"] == state
                        and (identifier is None or self.server.music_state["track"].get("id") == identifier))
        self.assertFalse(set(self.server.music_state["track"]) - {"id", "title", "artist", "album"})

    def test_music_and_saved_audio_through_actual_native_acknowledgements(self):
        folder = self.base / "generated-music"
        nested = folder / "nested"
        nested.mkdir(parents=True)
        for path in (folder / "Fixture Alpha.wav", nested / "Fixture Beta.wav"):
            with wave.open(str(path), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(24000)
                stream.writeframes(b"\0\0" * 24000 * 30)
        self.http("/music/folder", {"path": str(folder)})
        self.eventually(lambda: self.server.music.status()["status"] == "ready")
        listing = self.http("/music")
        self.assertEqual(listing["total"], 2)
        alpha = self.http("/music/search?q=" + quote("Fixture Alpha"))["tracks"][0]
        beta = next(track for track in listing["tracks"] if track["id"] != alpha["id"])
        self.start_native()
        # Coach remains connecting throughout: native controls must already work.
        self.assertEqual(self.server.jobs.coach.state, "connecting")
        self.music_action("play", query="Fixture Alpha")
        self.music_state("playing", alpha["id"])
        self.music_action("pause")
        self.music_state("paused", alpha["id"])
        self.music_action("resume")
        self.music_state("playing", alpha["id"])
        self.music_action("stop")
        self.music_state("stopped")
        self.music_action("play", track_id=beta["id"])
        self.music_state("playing", beta["id"])
        self.music_action("next")
        self.music_state("playing", alpha["id"])
        before_selection = self.http("/controls?after=0")["last_seq"]
        ambiguous = self.server.voice_action("music_control", {"action": "play", "query": "Fixture"})
        self.assertEqual(ambiguous["status"], "needs_selection")
        self.assertEqual(len(ambiguous["tracks"]), 2)
        missing = self.server.voice_action("music_control", {"action": "play", "query": "nonexistent song"})
        self.assertEqual(missing["status"], "failed")
        missing_id = self.server.voice_action("music_control", {"action": "play", "track_id": "not-indexed"})
        self.assertEqual(missing_id["status"], "failed")
        self.assertEqual(self.http("/controls?after=0")["last_seq"], before_selection,
                         "Ambiguous or unavailable selections must not enqueue guessed playback")
        self.music_state("playing", alpha["id"])
        self.music_action("stop")
        self.music_state("stopped")
        self.assertEqual(self.server.voice_action("music_control", {"action": "resume"})["status"], "failed")
        self.assertEqual(self.server.voice_action("music_control", {"action": "pause"})["status"], "failed")
        for channel, operation, value in (("voice", "set", 150), ("voice", "increase", 20),
                                          ("music", "set", 50), ("music", "decrease", 15), ("game", "set", 65)):
            result = self.server.voice_action("set_audio_volume", {"channel": channel, "operation": operation, "value": value})
            self.assertEqual(result["status"], "completed", result)
        expected = {"voice": 170, "music": 35, "game": 65}
        self.eventually(lambda: self.server.audio_state == expected)
        saved = json.loads(self.settings_path.read_text())
        for channel, value in expected.items():
            self.assertAlmostEqual(saved[channel + "_volume"], value / 100)
        self.close_native()
        self.assertNotIn("SCRIPT ERROR:", (self.base / "native.log").read_text())
        self.assertIn("NATIVE_MUSIC_HTTP_PASS", (self.base / "native.log").read_text())
        # Recreate both processes just as a normal app restart would.
        self.close_server()
        self.start_server()
        self.start_native()
        self.eventually(lambda: self.server.audio_state == expected)
        self.assertEqual(self.http("/music")["total"], 2, "The selected library must persist across restart")
        self.music_state("stopped")
        self.close_native()
        self.assertIn("NATIVE_MUSIC_HTTP_PASS", (self.base / "native.log").read_text())
