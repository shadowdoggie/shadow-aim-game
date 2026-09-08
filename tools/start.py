#!/usr/bin/env python3
"""Prepare missing desktop dependencies, then start Shadow Aim."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools import setup
from tools.runtime import default_data_dir, default_state_dir

LOG = logging.getLogger("shadow_aim.start")


def audio_ready(data: Path) -> bool:
    managed = data / ("runtime/venv/Scripts/python.exe" if os.name == "nt" else "runtime/venv/bin/python")
    bundled = ROOT / "runtime/python/python.exe"
    python = managed if managed.is_file() else bundled if bundled.is_file() else Path(sys.executable)
    result = subprocess.run([str(python), "-c", "from companion.voice_bridge import _load_media; _load_media()"],
                            cwd=ROOT, capture_output=True, timeout=30)
    return result.returncode == 0


def codex_ready(data: Path) -> bool:
    search = os.pathsep.join((str(data / "runtime/bin"), str(Path.home() / ".local/bin"), os.environ.get("PATH", "")))
    return shutil.which("codex", path=search) is not None


def prepare(data: Path) -> list[str]:
    """Retry missing pieces independently; failed setup never prevents offline play."""
    failures = []
    for name, ready, install in (("Voice and music", audio_ready, setup.install_audio),
                                 ("ChatGPT coaching", codex_ready, setup.install_codex)):
        try:
            if not ready(data):
                print(f"Setting up {name.lower()} for the first launch. Internet is required…", flush=True)
                LOG.info("Installing missing dependency: %s", name)
                install(data)
                if not ready(data):
                    raise RuntimeError("Installation finished, but the dependency is still unavailable.")
                LOG.info("Dependency ready: %s", name)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            failures.append(name)
            LOG.exception("Setup failed: %s", name)
            print(f"{name} setup could not finish: {error}\nWe will try again next time you start Shadow Aim.", flush=True)

    # A move or freshly extracted release gets a shortcut pointing at this folder.
    marker = data / "runtime/launcher.json"
    expected = {"version": 1, "folder": str(ROOT)}
    try:
        try:
            installed = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            installed = None
        if installed != expected:
            setup.desktop_shortcut(data)
            marker.parent.mkdir(parents=True, exist_ok=True)
            pending = marker.with_suffix(".tmp")
            pending.write_text(json.dumps(expected), encoding="utf-8")
            pending.replace(marker)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        LOG.exception("Could not install app-menu shortcut")
        print("The app-menu shortcut could not be added. You can keep using Start Shadow Aim.", flush=True)
    return failures


def main() -> int:
    if sys.version_info < (3, 11):
        print("Shadow Aim requires Python 3.11 or newer.", file=sys.stderr)
        return 1
    data = Path(os.environ.get("AIMCOACH_DATA_DIR", default_data_dir())).expanduser().resolve()
    state = Path(os.environ.get("AIMCOACH_STATE_DIR", default_state_dir())).expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    logging.basicConfig(filename=state / "launcher.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if prepare(data):
        print("Starting practice with the available features. Sign in inside the game when coaching is ready.", flush=True)
    # Keep setup exclusive to the release launcher. Direct source launches and
    # native tests retain their existing network-free startup behavior.
    import launch
    return launch.main()


if __name__ == "__main__":
    raise SystemExit(main())
