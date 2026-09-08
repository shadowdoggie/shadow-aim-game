#!/usr/bin/env python3
"""Build and smoke-check the small native game pack using the installed Godot."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

try:
    from .runtime import GODOT_VERSION, RUNTIMES, godot_notices, godot_runtime, windows_python
except ImportError:
    from runtime import GODOT_VERSION, RUNTIMES, godot_notices, godot_runtime, windows_python

ROOT = Path(__file__).resolve().parents[1]
SNAP_ENGINE = Path("/snap/godot-4/current/godot-4")


def run_checked(command: list[str], env: dict[str, str], cwd: Path, export_pack: Path | None = None) -> None:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True,
                            timeout=180)
    output = result.stdout + result.stderr
    plain = re.sub(r"\x1b\[[0-9;]*m", "", output)
    errors = [line.strip() for line in plain.splitlines() if "ERROR:" in line]
    known_export_shutdown = (export_pack is not None and export_pack.is_file() and export_pack.stat().st_size > 0
        and result.returncode == -6 and re.search(r"\[\s*DONE\s*\]\s*savepack", plain)
        and errors == ['ERROR: Parameter "singleton" is null.']
        and "at: is_cmdline_mode (editor/editor_node.cpp:" in plain
        and all(message not in plain for message in ("SCRIPT ERROR:", "Parse Error:", "Failed to load", "Resource file not found")))
    if known_export_shutdown:
        print("Godot hit its known editor-shutdown assertion after completing export; validating the generated pack in a separate runtime.")
        return
    if result.returncode or errors or "Parse Error:" in output or "Failed to load script" in output:
        raise RuntimeError(f"Godot exited with status {result.returncode}:\n" + (plain.strip()[-9000:] or "No diagnostic output"))
    for line in output.splitlines():
        if "WARNING:" in line or "ERROR:" in line:
            print(line)


def source_snapshot() -> dict[str, tuple[int, int]]:
    paths = [ROOT / "project.godot", ROOT / "export_presets.cfg", ROOT / "launch.py",
             *(ROOT / "game").rglob("*"), *(ROOT / "companion").rglob("*.py"),
             *(ROOT / "tools").glob("*.py"), ROOT / "INSTALL.md", ROOT / "PRIVACY.md", ROOT / "LICENSE"]
    return {str(path): (path.stat().st_mtime_ns, path.stat().st_size) for path in paths
            if path.is_file() and path.suffix != ".import"}


def make_release(pack: Path, target: str, build: Path, host_engine: str, env: dict) -> Path:
    """Stage only application files; never package personal history or credentials."""
    runtime = godot_runtime(build / "runtime-cache", target)
    with tempfile.TemporaryDirectory(prefix=".release-", dir=build) as temporary:
        stage = Path(temporary) / "shadow-aim"
        stage.mkdir()
        (stage / "runtime").mkdir()
        shutil.copy2(runtime, stage / "runtime" / runtime.name)
        shutil.copy2(pack, stage / "game.pck")
        for relative in ("launch.py", "INSTALL.md", "PRIVACY.md", "tools/setup.py", "tools/start.py", "tools/runtime.py", "tools/windows_audio.json",
                         "companion/voice_bridge/requirements.txt"):
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        if (ROOT / "LICENSE").is_file():
            shutil.copy2(ROOT / "LICENSE", stage / "LICENSE")
        for source in (ROOT / "companion").rglob("*.py"):
            if source.name.startswith("test_") or "__pycache__" in source.parts:
                continue
            destination = stage / source.relative_to(ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        godot_notices(stage / "licenses")
        if target.startswith("windows"):
            windows_python(build / "runtime-cache", stage / "runtime/python")
            (stage / "Start Shadow Aim.cmd").write_text('@echo off\ncd /d "%~dp0"\n'
                '"runtime\\python\\python.exe" tools\\start.py %*\n'
                'if errorlevel 1 (echo See INSTALL.md for help. & pause)\n',
                encoding="utf-8", newline="\r\n")
        else:
            script = stage / "Start Shadow Aim.sh"
            script.write_text('#!/bin/sh\nset -eu\ncd -- "$(dirname -- "$0")"\n'
                              'exec python3 "./tools/start.py" "$@"\n')
            script.chmod(0o755)
        (stage / "release.json").write_text(json.dumps({"platform": target, "godot": GODOT_VERSION,
            "python_minimum": "3.11", "voice": "GPT-Live/Juniper access is account-dependent and experimental"}, indent=2) + "\n")
        manifest = []
        for file in sorted(stage.rglob("*")):
            if file.is_file():
                with file.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                manifest.append(f"{digest}  {file.relative_to(stage).as_posix()}")
        (stage / "SHA256SUMS").write_text("\n".join(manifest) + "\n")
        # The pack is portable. Smoke-check it without the source tree; native
        # Windows execution is performed by Windows CI, never claimed on Linux.
        check_engine = str(stage / "runtime" / runtime.name) if target.startswith(platform.system().lower()) else host_engine
        smoke_env = {**env, "XDG_DATA_HOME": str(Path(temporary) / "smoke-data"),
                     "XDG_CONFIG_HOME": str(Path(temporary) / "smoke-config"),
                     "AIMCOACH_DATA_DIR": str(Path(temporary) / "smoke-companion"),
                     "AIMCOACH_STATE_DIR": str(Path(temporary) / "smoke-logs")}
        smoke_env.pop("AIMCOACH_PORT", None)
        smoke_env.pop("AIMCOACH_TOKEN", None)
        run_checked([check_engine, "--headless", "--audio-driver", "Dummy", "--main-pack",
                     str(stage / "game.pck"), "--quit-after", "10"], smoke_env, stage)
        subprocess.run([sys.executable, "-c", "import companion.server"], cwd=stage,
                       check=True, timeout=30, env={**env, "PYTHONPATH": str(stage), "PYTHONDONTWRITEBYTECODE": "1"})
        archive = build / (f"shadow-aim-{target}.zip" if target.startswith("windows") else f"shadow-aim-{target}.tar.gz")
        pending = archive.with_suffix(archive.suffix + ".part")
        try:
            if target.startswith("windows"):
                with zipfile.ZipFile(pending, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
                    for file in sorted(stage.rglob("*")):
                        if file.is_file():
                            output.write(file, "shadow-aim/" + file.relative_to(stage).as_posix())
            else:
                def anonymize(member):
                    member.uid = member.gid = 0
                    member.uname = member.gname = ""
                    member.mtime = 0
                    return member
                with tarfile.open(pending, "w:gz", compresslevel=6) as output:
                    output.add(stage, arcname="shadow-aim", filter=anonymize)
            os.replace(pending, archive)
        finally:
            pending.unlink(missing_ok=True)
        return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--godot", help="Godot 4 executable; defaults to GODOT_BIN or the installed engine")
    parser.add_argument("--release", action="store_true", help="build a distributable archive with a verified standard Godot runtime")
    parser.add_argument("--target", choices=tuple(RUNTIMES), default="linux-x86_64", help="release platform (pack contents work on either desktop OS)")
    args = parser.parse_args()
    candidates = [args.godot, os.environ.get("GODOT_BIN"),
                  str(SNAP_ENGINE) if SNAP_ENGINE.is_file() else None,
                  shutil.which("godot-4"), shutil.which("godot4"), shutil.which("godot")]
    engine = next((item for item in candidates if item and Path(item).is_file()), None)
    if not engine:
        parser.error("Godot 4 was not found. Supply --godot /path/to/godot.")
    engine = str(Path(engine).resolve())
    env = os.environ.copy()
    if str(engine).startswith("/snap/godot-4/"):
        env["DOTNET_ROOT"] = "/snap/godot-4/current/usr/lib/dotnet"
        env["PATH"] = env["DOTNET_ROOT"] + os.pathsep + env.get("PATH", "")
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    target = build / "game.pck"
    try:
        with tempfile.TemporaryDirectory(prefix=".pack-", dir=build) as temporary:
            staging = Path(temporary)
            pack = staging / "game.pck"
            sources = source_snapshot()
            print("Exporting native game data…", flush=True)
            run_checked([engine, "--headless", "--editor", "--path", str(ROOT),
                         "--export-pack", "Native Linux", str(pack)], env, ROOT, export_pack=pack)
            if not pack.is_file() or not pack.stat().st_size:
                raise RuntimeError("Godot did not create a game pack")
            # Use a separate working directory: the smoke check must load resources
            # from the pack, not accidentally fall back to the project source tree.
            print("Checking the packaged game…", flush=True)
            pack_env = {**env, "XDG_DATA_HOME": str(staging / "smoke-data"),
                        "APPDATA": str(staging / "smoke-roaming"), "LOCALAPPDATA": str(staging / "smoke-local")}
            pack_env.pop("AIMCOACH_TOKEN", None)
            pack_env.pop("AIMCOACH_PORT", None)
            run_checked([engine, "--headless", "--audio-driver", "Dummy", "--main-pack", str(pack),
                         "--quit-after", "10"], pack_env, staging)
            if source_snapshot() != sources:
                raise RuntimeError("Game sources changed during packaging. Run this command again.")
            os.replace(pack, target)
        print(f"Built {target} ({target.stat().st_size / 1024:.0f} KiB)")
        if args.release:
            sources = source_snapshot()
            archive = make_release(target, args.target, build, engine, env)
            if source_snapshot() != sources:
                archive.unlink(missing_ok=True)
                raise RuntimeError("Application sources changed during release packaging. Run the command again.")
            print(f"Built {archive} ({archive.stat().st_size / 1024 / 1024:.1f} MiB)")
        print("Run: ./launch.py --main-pack build/game.pck")
        print("This data pack uses your installed Godot runtime and local companion.")
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        print(f"Packaging failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
