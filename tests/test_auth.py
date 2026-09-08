import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from companion.auth import AppAuth, AuthError, _auth_command, codex_launch_prefix, shared_codex_env


class FakeRpc:
    def __init__(self, owner, environment, private):
        self.owner, self.environment, self.private = owner, environment, private
        self.home = Path(environment["CODEX_HOME"])
        self.calls, self.events = [], []
        self.closed = False

    def request(self, method, params=None):
        self.calls.append((method, params or {}))
        if method == "account/read":
            return {"account": self.owner.accounts.get(str(self.home)), "requiresOpenaiAuth": True}
        if method == "account/login/start":
            if self.owner.failure:
                raise AuthError("ChatGPT could not complete that account action. Please retry.")
            self.owner.attempt += 1
            if params["type"] == "chatgptDeviceCode":
                return {"type": "chatgptDeviceCode", "loginId": f"attempt-{self.owner.attempt}",
                        "verificationUrl": self.owner.url, "userCode": "TEST-1234"}
            return {"type": "chatgpt", "loginId": f"attempt-{self.owner.attempt}", "authUrl": self.owner.url}
        if method == "account/login/cancel":
            return {"status": "canceled"}
        if method == "account/logout":
            self.owner.accounts[str(self.home)] = None
            (self.home / "auth.json").unlink(missing_ok=True)
            return {}
        raise AssertionError(method)

    def notifications(self):
        result, self.events = self.events, []
        return result

    def close(self):
        self.closed = True


class FakeFactory:
    def __init__(self):
        self.instances = []
        self.accounts = {}
        self.url = "https://auth.openai.com/authorize?state=test-fixture"
        self.failure = False
        self.attempt = 0

    def __call__(self, environment, private):
        rpc = FakeRpc(self, environment, private)
        self.instances.append(rpc)
        return rpc

    def complete(self, auth, success=True):
        rpc = self.instances[-1]
        if success:
            self.accounts[str(rpc.home)] = {"type": "chatgpt", "email": "new-player@example.test", "planType": "plus"}
            (rpc.home / "auth.json").write_text('{"fixture": "not-a-real-credential"}')
            (rpc.home / "auth.json").chmod(0o644)
        rpc.events.append({"method": "account/login/completed", "params": {
            "loginId": auth._login["login_id"], "success": success, "error": None if success else "raw-server-detail"}})


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.external = self.root / "external-codex"
        self.external.mkdir()
        self.external_auth = self.external / "auth.json"
        self.external_auth.write_text('{"fixture": "external-sign-in-must-remain"}')
        self.original_auth = self.external_auth.read_bytes()
        self.environment = {"CODEX_HOME": str(self.external), "PATH": os.environ.get("PATH", ""),
                            "OPENAI_API_KEY": "fixture-api-key", "CODEX_ACCESS_TOKEN": "fixture-access-token"}
        self.factory = FakeFactory()
        self.factory.accounts[str(self.external)] = {"type": "chatgpt", "email": "player@example.test", "planType": "pro"}
        self.data = self.root / "data"
        self.auth = AppAuth(self.data, base_env=self.environment, rpc_factory=self.factory)

    def tearDown(self):
        self.auth.close()
        self.temporary.cleanup()

    def test_existing_login_remains_read_only_until_explicit_action(self):
        status = self.auth.status(refresh=True)
        self.assertEqual(status["state"], "signed_in")
        self.assertEqual(status["source"], "existing_codex")
        self.assertEqual(status["account"], {"email": "player@example.test", "plan_type": "pro"})
        self.assertEqual(self.factory.instances[0].calls, [("account/read", {"refreshToken": False})])
        self.assertEqual(self.external_auth.read_bytes(), self.original_auth)
        self.assertFalse((self.data / "auth/codex/auth.json").exists())

    def test_logout_never_calls_logout_on_external_codex_or_reimports_it(self):
        self.auth.status()
        result = self.auth.logout()
        self.assertEqual(result["state"], "signed_out")
        self.assertEqual(result["source"], "app")
        self.assertFalse(any(method == "account/logout" for rpc in self.factory.instances for method, _ in rpc.calls))
        self.assertEqual(self.external_auth.read_bytes(), self.original_auth)
        replacement = AppAuth(self.data, base_env=self.environment, rpc_factory=self.factory)
        try:
            self.assertEqual(replacement.status()["state"], "signed_out")
            self.assertEqual(replacement.status()["source"], "app")
        finally:
            replacement.close()

    def test_browser_login_completion_reads_account_and_keeps_tokens_private(self):
        started = self.auth.start_login()
        self.assertEqual(started["state"], "pending")
        self.assertEqual(started["login"]["url"], self.factory.url)
        rpc = self.factory.instances[-1]
        self.assertTrue(rpc.private)
        self.assertEqual(rpc.home, (self.data / "auth/codex").resolve())
        self.factory.complete(self.auth)
        result = self.auth.status()
        self.assertEqual(result["state"], "signed_in")
        self.assertIsNone(result["login"])
        self.assertEqual(result["account"]["plan_type"], "plus")
        self.assertNotIn("not-a-real-credential", json.dumps(result))
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE((rpc.home / "auth.json").stat().st_mode), 0o600)
        self.assertEqual(self.external_auth.read_bytes(), self.original_auth)

    def test_private_logout_uses_supported_rpc_and_removes_only_app_cache(self):
        self.auth.start_login()
        self.factory.complete(self.auth)
        self.auth.status()
        result = self.auth.logout()
        self.assertEqual(result["state"], "signed_out")
        logout_rpcs = [rpc for rpc in self.factory.instances if any(method == "account/logout" for method, _ in rpc.calls)]
        self.assertEqual(len(logout_rpcs), 1)
        self.assertTrue(logout_rpcs[0].private)
        self.assertFalse((self.data / "auth/codex/auth.json").exists())
        self.assertEqual(self.external_auth.read_bytes(), self.original_auth)

    def test_cancel_closes_login_process_and_ignores_late_notification(self):
        self.auth.start_login()
        pending = self.factory.instances[-1]
        login_id = self.auth._login["login_id"]
        self.assertEqual(self.auth.cancel_login()["state"], "signed_out")
        self.assertTrue(pending.closed)
        self.assertIn(("account/login/cancel", {"loginId": login_id}), pending.calls)
        pending.events.append({"method": "account/login/completed", "params": {"loginId": login_id, "success": True}})
        self.assertEqual(self.auth.status(refresh=True)["state"], "signed_out")

    def test_device_flow_exposes_only_the_one_time_user_code(self):
        started = self.auth.start_login("device")
        self.assertEqual(started["login"]["user_code"], "TEST-1234")
        self.assertIn(("account/login/start", {"type": "chatgptDeviceCode"}), self.factory.instances[-1].calls)

    def test_failed_login_is_retryable_and_does_not_restore_external_scope(self):
        self.factory.failure = True
        failed = self.auth.start_login()
        self.assertEqual(failed["state"], "error")
        self.assertEqual(failed["source"], "app")
        self.assertEqual(self.external_auth.read_bytes(), self.original_auth)
        self.factory.failure = False
        self.assertEqual(self.auth.start_login()["state"], "pending")

    def test_completion_errors_are_sanitized(self):
        self.auth.start_login()
        self.factory.complete(self.auth, success=False)
        result = self.auth.status()
        self.assertEqual(result["state"], "error")
        self.assertNotIn("raw-server-detail", json.dumps(result))

    def test_untrusted_or_embedded_credentials_in_login_urls_are_rejected(self):
        for url in ("http://auth.openai.com/login", "https://example.com/login", "https://name:password@auth.openai.com/login"):
            self.factory.url = url
            result = self.auth.start_login()
            self.assertEqual(result["state"], "error")
            self.assertIsNone(result["login"])
            self.assertNotIn(url, result["error"])

    def test_scope_environment_is_fresh_private_and_strips_other_credentials(self):
        initial = shared_codex_env(self.data, self.environment)
        self.assertEqual(initial["CODEX_HOME"], str(self.external))
        self.auth.logout()
        private = shared_codex_env(self.data, self.environment)
        self.assertEqual(private["CODEX_HOME"], str((self.data / "auth/codex").resolve()))
        self.assertNotIn("OPENAI_API_KEY", private)
        self.assertNotIn("CODEX_ACCESS_TOKEN", private)
        self.assertIn("OPENAI_API_KEY", self.environment)
        if os.name == "posix":
            for path in (self.data / "auth", self.data / "auth/codex"):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            for path in (self.data / "auth/scope.json", self.data / "auth/codex/config.toml"):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_read_only_compatibility_does_not_force_login_method(self):
        environment = shared_codex_env(self.data, self.environment)
        command = _auth_command("codex", environment, False)
        self.assertFalse(any("forced_login_method" in value for value in command))
        self.auth.logout()
        command = _auth_command("codex", shared_codex_env(self.data, self.environment), True)
        self.assertIn('forced_login_method="chatgpt"', command)
        self.assertIn('cli_auth_credentials_store="file"', command)

    def test_api_key_login_does_not_count_as_subscription_login(self):
        self.factory.accounts[str(self.external)] = {"type": "apiKey"}
        status = self.auth.status()
        self.assertEqual(status["state"], "error")
        self.assertIsNone(status["account"])
        self.assertIn("ChatGPT", status["error"])
        self.assertEqual(self.external_auth.read_bytes(), self.original_auth)

    def test_shared_credential_symlink_cannot_be_treated_as_app_owned(self):
        try:
            (self.data / "auth/codex/auth.json").symlink_to(self.external_auth)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows has not granted symbolic-link creation to this process")
            raise
        with self.assertRaises(AuthError):
            shared_codex_env(self.data, self.environment)
        self.assertEqual(self.external_auth.read_bytes(), self.original_auth)

    def test_corrupt_scope_does_not_silently_adopt_external_credentials(self):
        (self.data / "auth/scope.json").write_text("[]")
        with self.assertRaises(AuthError):
            shared_codex_env(self.data, self.environment)

    def test_expired_login_is_cancelled_without_touching_other_accounts(self):
        self.auth.start_login()
        self.auth._login_started -= 601
        status = self.auth.status()
        self.assertEqual(status["state"], "error")
        self.assertIsNone(status["login"])
        self.assertIn("expired", status["error"])
        self.assertEqual(self.external_auth.read_bytes(), self.original_auth)

    def test_windows_npm_shim_uses_node_without_a_command_shell(self):
        install = self.root / "Codex Déjà install"
        script = install / "node_modules/@openai/codex/bin/codex.js"
        script.parent.mkdir(parents=True)
        script.write_text("// fixture", encoding="utf-8")
        shim = install / "codex.cmd"
        shim.write_text("@echo fixture")
        node = install / "node.exe"
        node.touch()
        command = codex_launch_prefix(str(shim), self.environment)
        self.assertEqual(command, [str(node), str(script)])
        self.assertNotIn("cmd.exe", command)

    def test_native_executable_path_and_incomplete_cmd_are_not_shell_commands(self):
        native = self.root / "native codex.exe"
        native.touch()
        self.assertEqual(codex_launch_prefix(str(native), self.environment), [str(native)])
        with patch("companion.auth.shutil.which", return_value=None):
            with self.assertRaises(AuthError):
                codex_launch_prefix(str(self.root / "missing.cmd"), self.environment)


if __name__ == "__main__":
    unittest.main()
