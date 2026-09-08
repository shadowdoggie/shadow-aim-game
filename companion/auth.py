"""ChatGPT account control with an app-owned Codex credential scope.

Existing installations can keep using their current Codex sign-in until their
first explicit account action. That compatibility scope is read-only here:
login and logout always switch permanently to this app's private CODEX_HOME.
"""
from __future__ import annotations

from collections import deque
import copy
import json
import logging
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
from urllib.parse import urlsplit

LOG = logging.getLogger(__name__)
_BLOCKED_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL",
                "CODEX_ACCESS_TOKEN", "OPENAI_ACCESS_TOKEN", "CODEX_AUTH_JSON")
_LOGIN_HOSTS = {"auth.openai.com", "chatgpt.com", "auth.chatgpt.com"}


class AuthError(RuntimeError):
    """A safe account error suitable for the native UI."""


def _write_private_json(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".account-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _scope(data_dir: Path, environment: dict) -> dict:
    auth_dir = Path(data_dir) / "auth"
    private_home = auth_dir / "codex"
    for directory in (auth_dir, private_home):
        if directory.is_symlink():
            raise AuthError("Shadow Aim account storage must be a separate folder.")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    config = private_home / "config.toml"
    if config.is_symlink() or (private_home / "auth.json").is_symlink():
        raise AuthError("Shadow Aim account files must be separate from other Codex accounts.")
    if not config.exists():
        descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write('cli_auth_credentials_store = "file"\nforced_login_method = "chatgpt"\n')
    config.chmod(0o600)
    state_path = auth_dir / "scope.json"
    if not state_path.exists():
        external_home = Path(environment.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
        has_existing = (external_home / "auth.json").is_file() or (external_home / "config.toml").is_file()
        mode = "existing_codex" if has_existing and external_home != private_home.resolve() else "app"
        _write_private_json(state_path, {"mode": mode, "existing_home": str(external_home)})
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AuthError("The saved account settings could not be read. Practice is still available.") from error
    if (not isinstance(state, dict) or state.get("mode") not in ("existing_codex", "app") or
            not isinstance(state.get("existing_home"), str) or not Path(state["existing_home"]).is_absolute()):
        raise AuthError("The saved account settings are invalid. Practice is still available.")
    state_path.chmod(0o600)
    return state


def shared_codex_env(data_dir: str | Path, base_env: dict | None = None) -> dict:
    """Return a fresh spawn environment; callers must not cache it across logout."""
    environment = dict(os.environ if base_env is None else base_env)
    state = _scope(Path(data_dir), environment)
    environment["CODEX_HOME"] = (state["existing_home"] if state["mode"] == "existing_codex"
                                 else str((Path(data_dir) / "auth" / "codex").resolve()))
    for key in _BLOCKED_ENV:
        environment.pop(key, None)
    return environment


def codex_launch_prefix(binary: str | None = None, environment: dict | None = None) -> list[str]:
    """Resolve native Codex or npm's JavaScript entry without invoking cmd.exe."""
    environment = os.environ if environment is None else environment
    search_path = environment.get("PATH", "")
    if binary:
        selected = shutil.which(binary, path=search_path) or binary
    else:
        selected = (shutil.which("codex.exe", path=search_path) if os.name == "nt" else None)
        selected = selected or shutil.which("codex", path=search_path)
        fallback = Path.home() / ".local/bin/codex"
        if not selected and os.name != "nt" and fallback.is_file():
            selected = str(fallback)
        if not selected:
            raise AuthError("Codex was not found. Run Shadow Aim setup, then retry.")
    path = Path(selected)
    if path.suffix.lower() in (".cmd", ".bat", ".js"):
        script = path if path.suffix.lower() == ".js" else path.parent / "node_modules/@openai/codex/bin/codex.js"
        local_node = path.parent / "node.exe"
        node = str(local_node) if local_node.is_file() else shutil.which("node", path=search_path)
        if not script.is_file() or not node:
            raise AuthError("The Codex Windows launcher is incomplete. Run Shadow Aim setup, then retry.")
        return [node, str(script)]
    return [str(selected)]


def _auth_command(binary: str | None, environment: dict, private: bool) -> list[str]:
    options = {"approval_policy": "never", "sandbox_mode": "read-only",
               "web_search": "disabled", "project_doc_max_bytes": 0,
               "apps._default.enabled": False}
    for feature in ("shell_tool", "unified_exec", "apps", "plugins", "remote_plugin",
                    "browser_use", "browser_use_external", "computer_use", "memories",
                    "hooks", "multi_agent", "multi_agent_v2", "skill_search"):
        options[f"features.{feature}"] = False
    options["features.skip_host_skill_discovery"] = True
    if private:
        options.update(forced_login_method="chatgpt", cli_auth_credentials_store="file")
    # Never impose a login-method override on the existing Codex scope: that can
    # cause Codex to discard an incompatible external account at startup.
    config = Path(environment["CODEX_HOME"]) / "config.toml"
    if config.is_file():
        try:
            values = tomllib.loads(config.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise AuthError("Codex account configuration could not be read.") from error
        for section in ("mcp_servers", "plugins"):
            for name in values.get(section, {}):
                options[f"{section}.{name}.enabled"] = False
    command = [*codex_launch_prefix(binary, environment), "app-server", "--stdio"]
    for key, value in options.items():
        command.extend(("-c", f"{key}={json.dumps(value)}"))
    return command


class _AuthRpc:
    def __init__(self, environment: dict, private: bool):
        self.process = None
        self._temporary = tempfile.TemporaryDirectory(prefix="shadow-aim-account-")
        self._events: queue.Queue = queue.Queue()
        self._notifications: deque = deque(maxlen=128)
        self._next_id = 0
        try:
            self.process = subprocess.Popen(_auth_command(None, environment, private),
                cwd=self._temporary.name, env=environment, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self._reader = threading.Thread(target=self._read, daemon=True, name="shadow-aim-account")
            self._reader.start()
            self.request("initialize", {"clientInfo": {"name": "shadow_aim_account", "version": "0.1.0"}})
            self._send({"method": "initialized", "params": {}})
        except (OSError, AuthError) as error:
            self.close()
            raise AuthError("The Codex account connection could not start. Install Codex or retry.") from error

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self._events.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            self._events.put({"_closed": True})

    def _send(self, payload: dict):
        try:
            self.process.stdin.write(json.dumps(payload) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as error:
            raise AuthError("The Codex account connection closed. Please retry.") from error

    def request(self, method: str, params: dict | None = None, timeout: float = 20) -> dict:
        self._next_id += 1
        identifier = self._next_id
        self._send({"id": identifier, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = self._events.get(timeout=min(0.2, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if event.get("_closed"):
                raise AuthError("The Codex account connection closed. Please retry.")
            if event.get("id") == identifier and "method" not in event:
                if event.get("error"):
                    LOG.warning("account_rpc_failed method=%s code=%s", method, event["error"].get("code"))
                    raise AuthError("ChatGPT could not complete that account action. Please retry.")
                result = event.get("result", {})
                return result if isinstance(result, dict) else {}
            self._notification(event)
        raise AuthError("The account request timed out. Please retry.")

    def _notification(self, event: dict):
        if "method" in event and "id" in event:
            self._send({"id": event["id"], "error": {"code": -32601,
                        "message": "Account control does not support tool requests."}})
        elif "method" in event:
            self._notifications.append(event)

    def notifications(self) -> list[dict]:
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            if event.get("_closed"):
                raise AuthError("The Codex account connection closed. Please retry.")
            self._notification(event)
        result = list(self._notifications)
        self._notifications.clear()
        return result

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    stream.close()
            self.process = None
        self._temporary.cleanup()


class AppAuth:
    """Serialized managed login; callers stop inference/voice before account actions."""
    def __init__(self, data_dir: str | Path, *, base_env: dict | None = None, rpc_factory=None):
        self.data_dir = Path(data_dir)
        self._environment = dict(os.environ if base_env is None else base_env)
        self._lock = threading.RLock()
        self._rpc_factory = rpc_factory or _AuthRpc
        self._rpc = None
        self._state = _scope(self.data_dir, self._environment)
        self._account = None
        self._login = None
        self._login_started = 0.0
        self._error = None
        self._last_read = 0.0
        self._closed = False

    def _connection(self):
        if self._closed:
            raise AuthError("Account control has closed.")
        if self._rpc is None:
            self._rpc = self._rpc_factory(shared_codex_env(self.data_dir, self._environment),
                                          self._state["mode"] == "app")
        return self._rpc

    def _read_account(self):
        response = self._connection().request("account/read", {"refreshToken": False})
        account = response.get("account") or {}
        if account.get("type") == "chatgpt":
            self._account = {"email": account.get("email"), "plan_type": account.get("planType", "unknown")}
            self._error = None
        else:
            self._account = None
            if account.get("type"):
                self._error = "Sign in with ChatGPT to use coaching. API-key billing is not supported."
        self._last_read = time.monotonic()
        self._secure_credentials()

    def _secure_credentials(self):
        path = self.data_dir / "auth" / "codex" / "auth.json"
        if path.is_file() and not path.is_symlink():
            path.chmod(0o600)

    def _result(self) -> dict:
        return {"state": "pending" if self._login else ("signed_in" if self._account else
                ("error" if self._error else "signed_out")),
                "account": copy.deepcopy(self._account), "login": copy.deepcopy(self._login),
                "source": self._state["mode"], "error": self._error,
                "availability_note": "Coaching and Juniper depend on your plan's model access and usage limits."}

    def status(self, refresh: bool = False) -> dict:
        with self._lock:
            try:
                if self._rpc is not None:
                    for event in self._rpc.notifications():
                        if event.get("method") == "account/login/completed":
                            params = event.get("params") or {}
                            if self._login and params.get("loginId") == self._login["login_id"]:
                                self._login = None
                                if params.get("success"):
                                    self._read_account()
                                    LOG.info("account_login_completed success=true scope=app")
                                else:
                                    self._error = "ChatGPT sign-in did not finish. Please try again."
                                    LOG.info("account_login_completed success=false scope=app")
                        elif event.get("method") == "account/updated":
                            self._last_read = 0
                if self._login and time.monotonic() - self._login_started > 600:
                    self.cancel_login()
                    self._error = "The sign-in link expired. Start sign-in again."
                if not self._login and (refresh or time.monotonic() - self._last_read > 10):
                    self._read_account()
            except AuthError as error:
                self._error = str(error)
                self._discard_connection()
            return self._result()

    def _use_private_scope(self):
        self._discard_connection()
        self._state = {**self._state, "mode": "app"}
        _write_private_json(self.data_dir / "auth" / "scope.json", self._state)
        self._account = None
        self._login = None
        self._error = None
        self._last_read = 0

    def start_login(self, flow: str = "browser") -> dict:
        if flow not in ("browser", "device"):
            raise AuthError("Choose browser or device sign-in.")
        with self._lock:
            if self._login:
                self.cancel_login()
            self._use_private_scope()
            try:
                response = self._connection().request("account/login/start",
                    {"type": "chatgpt"} if flow == "browser" else {"type": "chatgptDeviceCode"})
                url = response.get("authUrl") if flow == "browser" else response.get("verificationUrl")
                parsed = urlsplit(url or "")
                identifier = response.get("loginId")
                if (parsed.scheme != "https" or parsed.hostname not in _LOGIN_HOSTS or
                        parsed.username or parsed.password or not isinstance(identifier, str) or not identifier):
                    raise AuthError("ChatGPT returned an invalid sign-in link. Please retry.")
                self._login = {"login_id": identifier, "url": url,
                               "user_code": response.get("userCode") if flow == "device" else None}
                self._login_started = time.monotonic()
                LOG.info("account_login_started flow=%s scope=app", flow)
            except AuthError as error:
                self._error = str(error)
                self._discard_connection()
            return self._result()

    def cancel_login(self) -> dict:
        with self._lock:
            login, self._login = self._login, None
            if login and self._state["mode"] == "app":
                try:
                    self._connection().request("account/login/cancel", {"loginId": login["login_id"]})
                except AuthError:
                    pass
                self._discard_connection()
            self._error = None
            self._last_read = 0
            return self._result()

    def logout(self) -> dict:
        with self._lock:
            was_private = self._state["mode"] == "app"
            if self._login:
                self.cancel_login()
            self._use_private_scope()
            if was_private:
                try:
                    self._connection().request("account/logout", {})
                except AuthError:
                    # This file scope can still be disconnected locally offline.
                    LOG.info("account_logout_local_only scope=app")
                finally:
                    self._discard_connection()
            (self.data_dir / "auth" / "codex" / "auth.json").unlink(missing_ok=True)
            self._account = None
            self._error = None
            self._last_read = time.monotonic()
            LOG.info("account_logged_out scope=app external_account_unchanged=true")
            return self._result()

    def _discard_connection(self):
        if self._rpc is not None:
            self._rpc.close()
            self._rpc = None

    def close(self):
        with self._lock:
            if self._login:
                self.cancel_login()
            self._discard_connection()
            self._closed = True
