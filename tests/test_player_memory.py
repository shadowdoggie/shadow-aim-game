from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from companion.player_memory import MAX_NOTES, MemoryError, PlayerMemory


class PlayerMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = PlayerMemory(self.temp.name)
        self.scope = "account-one@example.invalid"

    def path(self, scope=None):
        digest = hashlib.sha256((scope or self.scope).encode()).hexdigest()
        return Path(self.temp.name) / "player-memory" / (digest + ".json")

    def values(self, scope=None):
        return {note["key"]: note["value"] for note in self.memory.get_context(scope or self.scope)["memories"]}

    def test_explicit_notes_survive_restart_and_are_isolated_by_account(self):
        self.memory.remember(self.scope, "preferred name", "Avery")
        self.memory.remember(self.scope, "favorite game", "Portal")
        other = "account-two@example.invalid"
        self.assertEqual(self.memory.get_context(other)["memories"], [])
        self.memory.remember(other, "preferred name", "Morgan")
        self.memory = PlayerMemory(self.temp.name)
        self.assertEqual(self.values(), {"preferred name": "Avery", "favorite game": "Portal"})
        self.assertEqual(self.values(other), {"preferred name": "Morgan"})
        self.assertNotIn(self.scope, self.path().read_text())
        self.assertNotIn("@", self.path().name)

    def test_corrections_replace_case_insensitive_key_and_preserve_creation_time(self):
        with patch("companion.player_memory.time.time", side_effect=[100.0, 200.0]):
            self.memory.remember(self.scope, "Favorite Game", "Portal")
            self.memory.remember(self.scope, " favorite   game ", "Celeste")
        document = json.loads(self.path().read_text())
        self.assertEqual(document["memories"], [{"key": "favorite game", "value": "Celeste",
                                               "created_at": 100.0, "updated_at": 200.0}])

    def test_forget_one_or_all_does_not_affect_other_account(self):
        other = "account-two@example.invalid"
        self.memory.remember(self.scope, "name", "Avery")
        self.memory.remember(self.scope, "goal", "Enjoy focused practice")
        self.memory.remember(other, "name", "Morgan")
        self.memory.forget(self.scope, "NAME")
        self.assertEqual(self.values(), {"goal": "Enjoy focused practice"})
        self.assertEqual(self.memory.forget(self.scope)["memories"], [])
        self.assertEqual(PlayerMemory(self.temp.name).get_context(self.scope)["count"], 0)
        self.assertEqual(self.values(other), {"name": "Morgan"})
        self.assertNotIn("Enjoy focused practice", self.path().read_text())

    def test_missing_account_never_uses_a_shared_default(self):
        for scope in (None, "", "  ", "default", "anonymous", "guest", "signed_out", "unknown"):
            with self.subTest(scope=scope):
                for operation in (lambda: self.memory.get_context(scope),
                                  lambda: self.memory.remember(scope, "name", "Avery"),
                                  lambda: self.memory.forget(scope)):
                    with self.assertRaises(MemoryError):
                        operation()
        self.assertEqual(list(self.memory.directory.iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "POSIX private permission bits")
    def test_files_and_directory_are_private(self):
        self.memory.remember(self.scope, "goal", "Learn patiently")
        self.assertEqual(stat.S_IMODE(self.memory.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path().stat().st_mode), 0o600)
        self.path().chmod(0o644)
        self.memory.get_context(self.scope)
        self.assertEqual(stat.S_IMODE(self.path().stat().st_mode), 0o600)

    def test_failed_atomic_replace_preserves_saved_notes_and_removes_temporary_file(self):
        self.memory.remember(self.scope, "favorite game", "Portal")
        before = self.path().read_bytes()
        with patch("companion.player_memory.os.replace", side_effect=OSError("injected disk error")):
            with self.assertRaises(MemoryError):
                self.memory.remember(self.scope, "favorite game", "Celeste")
        self.assertEqual(self.path().read_bytes(), before)
        self.assertEqual(list(self.memory.directory.iterdir()), [self.path()])

    def test_concurrent_updates_do_not_lose_notes(self):
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(lambda index: self.memory.remember(self.scope, f"interest {index}",
                                                                       f"Explicit preference {index}"), range(32)))
        self.assertEqual(len(results), 32)
        self.assertEqual(PlayerMemory(self.temp.name).get_context(self.scope, 32768)["count"], 32)

    def test_limits_reject_overflow_without_evicting_user_notes(self):
        for index in range(MAX_NOTES):
            self.memory.remember(self.scope, f"interest {index}", "x" * 300)
        before = self.path().read_bytes()
        with self.assertRaises(MemoryError):
            self.memory.remember(self.scope, "one more", "This must not replace a remembered fact")
        self.assertEqual(self.path().read_bytes(), before)
        self.memory.remember(self.scope, "interest 0", "A correction still fits")
        self.assertEqual(self.memory.get_context(self.scope, 32768)["count"], MAX_NOTES)
        for key, value in (("a" * 65, "ok"), ("good key", "x" * 301), ("good key", ""),
                           ("../outside", "no"), ("raw transcript", "text"),
                           ("api_key", "fake-key"), ("favorite game", "one\ntwo")):
            with self.subTest(key=key):
                with self.assertRaises(MemoryError):
                    self.memory.remember(self.scope, key, value)

    def test_obvious_credentials_are_rejected_without_writing_them(self):
        for value in ("sk-" + "a" * 30, "hf_" + "b" * 30, "Bearer " + "c" * 30,
                      "password: fictional-secret", "-----BEGIN PRIVATE KEY-----"):
            with self.assertRaises(MemoryError):
                self.memory.remember(self.scope, "personal note", value)
        self.assertEqual(list(self.memory.directory.iterdir()), [])

    def test_bounded_context_keeps_old_name_and_goals_without_truncating_facts(self):
        self.memory.remember(self.scope, "preferred name", "Avery")
        self.memory.remember(self.scope, "goal", "Enjoy practicing")
        for index in range(20):
            self.memory.remember(self.scope, f"interest {index}", "é" * 300)
        context = self.memory.get_context(self.scope, 1000)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 1000)
        self.assertTrue(context["truncated"])
        self.assertEqual(context["count"], 22)
        self.assertTrue({"preferred name", "goal"} <= {note["key"] for note in context["memories"]})
        self.assertTrue(all(note["value"] in {"Avery", "Enjoy practicing", "é" * 300} for note in context["memories"]))
        for limit in (0, 127, 32769, True, 1.5):
            with self.assertRaises(MemoryError):
                self.memory.get_context(self.scope, limit)

    def test_corrupt_data_is_not_silently_replaced_but_explicit_forget_all_works(self):
        self.path().write_text('{"broken":true}')
        before = self.path().read_bytes()
        with self.assertRaises(MemoryError):
            self.memory.remember(self.scope, "name", "Avery")
        self.assertEqual(self.path().read_bytes(), before)
        self.assertEqual(self.memory.forget(self.scope)["count"], 0)
        self.assertEqual(self.memory.get_context(self.scope)["memories"], [])

    def test_absent_forget_is_idempotent_and_does_not_create_account_files(self):
        self.assertEqual(self.memory.forget(self.scope, "unknown note")["count"], 0)
        self.assertEqual(self.memory.forget(self.scope)["count"], 0)
        self.assertEqual(list(self.memory.directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
