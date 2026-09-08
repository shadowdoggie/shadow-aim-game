#!/usr/bin/env python3
"""Start the native game and its private, local coaching companion."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import queue
import secrets
import shutil
import signal
import subprocess
import sys
import threading

from tools.runtime import default_data_dir, default_state_dir

ROOT = Path(__file__).resolve().parent


def terminate(signum: int, _frame: object) -> None:
    # A desktop/session shutdown must use the same child cleanup as normal exit.
    raise SystemExit(128 + signum)


def _handshake(process: subprocess.Popen, timeout: float = 20) -> str:
    # Windows selectors cannot monitor anonymous subprocess pipes.
    lines: queue.Queue = queue.Queue(maxsize=1)

    def read_ready():
        try:
            lines.put(process.stdout.readline())
        except (OSError, ValueError):
            lines.put("")

    threading.Thread(target=read_ready, daemon=True, name="shadow-aim-startup").start()
    try:
        return lines.get(timeout=timeout)
    except queue.Empty as error:
        raise RuntimeError("Local coaching companion did not start within 20 seconds") from error


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        # The companion owns Codex child processes; close its entire tree as well.
        taskkill = str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32/taskkill.exe")
        subprocess.run([taskkill, "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=5, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()


def main() -> int:
    if sys.version_info < (3, 11):
        print("Shadow Aim requires Python 3.11 or newer.", file=sys.stderr)
        return 1
    signal.signal(signal.SIGTERM, terminate)
    data = Path(os.environ.get("AIMCOACH_DATA_DIR", default_data_dir())).expanduser().resolve()
    managed_python = data / ("runtime/venv/Scripts/python.exe" if os.name == "nt" else "runtime/venv/bin/python")
    bundled_python = ROOT / "runtime/python/python.exe"
    preferred_python = managed_python if managed_python.is_file() else bundled_python
    if (preferred_python.is_file() and preferred_python.resolve() != Path(sys.executable).resolve()
            and os.environ.get("AIMCOACH_RUNTIME_ACTIVE") != "1"):
        environment = {**os.environ, "AIMCOACH_RUNTIME_ACTIVE": "1"}
        command = [str(preferred_python), str(ROOT / "launch.py"), *sys.argv[1:]]
        if os.name == "nt":
            return subprocess.call(command, env=environment)
        os.execve(preferred_python, command, environment)
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(data, 0o700)
    state = Path(os.environ.get("AIMCOACH_STATE_DIR", default_state_dir())).expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    logging.basicConfig(filename=state / "launcher.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
    candidates = [os.environ.get("GODOT_BIN"), str(ROOT / ("runtime/godot.exe" if os.name == "nt" else "runtime/godot")), "/snap/godot-4/current/godot-4", shutil.which("godot-4"),
                  shutil.which("godot4"), shutil.which("godot"), "/snap/bin/godot-4"]
    godot = next((p for p in candidates if p and Path(p).is_file()), None)
    if not godot:
        print("Godot 4 was not found. Use the complete release bundle or set GODOT_BIN to Godot 4.7.", file=sys.stderr)
        return 1
    env = os.environ.copy()
    if str(godot).startswith("/snap/godot-4/"):
        env["DOTNET_ROOT"] = "/snap/godot-4/current/usr/lib/dotnet"
        env["PATH"] = env["DOTNET_ROOT"] + os.pathsep + env.get("PATH", "")
    env["AIMCOACH_TOKEN"] = secrets.token_urlsafe(32)
    env["AIMCOACH_DATA_DIR"] = str(data)
    env["AIMCOACH_STATE_DIR"] = str(state)
    # The desktop launcher may not inherit the shell's ~/.local/bin PATH.
    env["PATH"] = os.pathsep.join((str(data / "runtime/bin"), str(Path.home() / ".local/bin"), env.get("PATH", "")))
    companion = None
    game = None
    child_options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    with (state / "companion.log").open("a", encoding="utf-8") as companion_log, (state / "game.log").open("a", encoding="utf-8") as game_log:
        try:
            companion = subprocess.Popen(
                [sys.executable, "-m", "companion.server", "--port", "0", "--data-dir", str(data)],
                cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=companion_log,
                text=True, encoding="utf-8", **child_options,
            )
            line = _handshake(companion)
            handshake = json.loads(line)
            env["AIMCOACH_PORT"] = str(int(handshake["port"]))
            logging.info("Companion ready on loopback port %s", env["AIMCOACH_PORT"])
            args = [godot, "--path", str(ROOT)]
            if not (ROOT / "project.godot").exists() and "--main-pack" not in sys.argv[1:]:
                if not (ROOT / "game.pck").is_file():
                    raise RuntimeError("The game data is missing. Extract the complete Shadow Aim bundle again")
                args.extend(("--main-pack", str(ROOT / "game.pck")))
            args.extend(sys.argv[1:])
            game = subprocess.Popen(args, cwd=ROOT, env=env, stdout=game_log,
                                    stderr=subprocess.STDOUT, **child_options)
            return game.wait()
        except (OSError, ValueError, RuntimeError) as exc:
            logging.exception("Aim Coach could not start")
            print(f"Aim Coach could not start: {exc}. Logs: {state}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130
        finally:
            for process in (game, companion):
                if process:
                    try:
                        _stop_process(process)
                    except (OSError, subprocess.TimeoutExpired):
                        logging.exception("Could not finish closing child process %s", process.pid)


if __name__ == "__main__":
    raise SystemExit(main())
