"""First-launch setup must be recoverable without blocking offline practice."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import start


class FirstLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(start.LOG, "exception"))
        self.audio_ready = self.stack.enter_context(patch.object(start, "audio_ready", return_value=True))
        self.codex_ready = self.stack.enter_context(patch.object(start, "codex_ready", return_value=True))
        self.audio = self.stack.enter_context(patch.object(start.setup, "install_audio"))
        self.codex = self.stack.enter_context(patch.object(start.setup, "install_codex"))
        self.shortcut = self.stack.enter_context(patch.object(start.setup, "desktop_shortcut"))

    def test_first_launch_installs_missing_dependencies_then_second_reuses_them(self):
        self.audio_ready.side_effect = [False, True, True]
        self.codex_ready.side_effect = [False, True, True]
        self.assertEqual(start.prepare(self.data), [])
        self.assertEqual(start.prepare(self.data), [])
        self.audio.assert_called_once_with(self.data)
        self.codex.assert_called_once_with(self.data)
        self.shortcut.assert_called_once_with(self.data)

    def test_failed_audio_does_not_block_codex_and_retries_on_next_launch(self):
        self.audio_ready.side_effect = [False, False, True]
        self.codex_ready.side_effect = [False, True, True]
        self.audio.side_effect = [RuntimeError("download interrupted"), None]
        self.assertEqual(start.prepare(self.data), ["Voice and music"])
        self.assertEqual(start.prepare(self.data), [])
        self.assertEqual(self.audio.call_count, 2)
        self.codex.assert_called_once_with(self.data)

    def test_installer_success_without_working_dependency_is_reported_as_failure(self):
        self.audio_ready.return_value = False
        self.assertEqual(start.prepare(self.data), ["Voice and music"])

    def test_ready_installation_does_not_download_dependencies(self):
        self.assertEqual(start.prepare(self.data), [])
        self.audio.assert_not_called()
        self.codex.assert_not_called()

    def test_shortcut_failure_retries_and_moved_release_updates_target(self):
        self.shortcut.side_effect = [OSError("unavailable"), None, None]
        self.assertEqual(start.prepare(self.data), [])
        self.assertFalse((self.data / "runtime/launcher.json").exists())
        start.prepare(self.data)
        marker = self.data / "runtime/launcher.json"
        self.assertEqual(json.loads(marker.read_text())["folder"], str(start.ROOT))
        marker.write_text(json.dumps({"version": 1, "folder": "old release"}))
        start.prepare(self.data)
        self.assertEqual(self.shortcut.call_count, 3)

    def test_main_starts_game_even_when_optional_setup_fails(self):
        with patch.dict(start.os.environ, {"AIMCOACH_DATA_DIR": str(self.data), "AIMCOACH_STATE_DIR": str(self.data)}), \
                patch.object(start, "prepare", return_value=["Voice and music"]), \
                patch("launch.main", return_value=0) as launch, patch.object(start.logging, "basicConfig"):
            self.assertEqual(start.main(), 0)
            launch.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
