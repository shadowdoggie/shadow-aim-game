#!/usr/bin/env python3
"""Check or install Shadow Aim's optional per-user dependencies and shortcut."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import tarfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.runtime import default_data_dir, download, install_windows_audio


def run(command, **kwargs):
    subprocess.run([str(x) for x in command], check=True, **kwargs)


def install_audio(data):
    environment = data / "runtime/venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if (ROOT / "runtime/python/python.exe").is_file():
        install_windows_audio(ROOT / "runtime/python/Lib/site-packages", ROOT / "tools/windows_audio.json")
        run([ROOT / "runtime/python/python.exe", "-c", "import aiortc, av; print('Native voice and music support ready.')"])
        return
    import venv
    if not python.exists():
        try:
            venv.EnvBuilder(with_pip=True).create(environment)
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeError("Python venv support is missing. On Ubuntu/Debian install python3-venv, then run setup again.") from error
    run([python, "-m", "pip", "install", "--disable-pip-version-check", "--only-binary=:all:",
         "-r", ROOT / "companion/voice_bridge/requirements.txt"])
    run([python, "-c", "import aiortc, av; print('Native voice and music support ready.')"])


def install_codex(data):
    # Never update or replace the user's unrelated Codex installation or login.
    binary = data / "runtime/bin" / ("codex.exe" if os.name == "nt" else "codex")
    search = os.pathsep.join((str(binary.parent), str(Path.home() / ".local/bin"), os.environ.get("PATH", "")))
    if shutil.which("codex", path=search):
        print("Codex is already installed; leaving its installation unchanged.")
        return
    install_codex_package(data, "windows-x86_64" if os.name == "nt" else "linux-x86_64")
    print("Codex installed for Shadow Aim. Sign in from the game's account controls.")


# Official release packages include Codex's companion executables and resources.
# Pin both URL and GitHub's published SHA256 instead of executing a remote script.
CODEX_VERSION = "rust-v0.153.4"
CODEX_PACKAGES = {
    "linux-x86_64": ("codex-package-x86_64-unknown-linux-musl.tar.gz",
        "a822187e1a2420c61c5926721bfbd878701ed95547c9bb0d4de4498a16ba1821", "codex"),
    "windows-x86_64": ("codex-package-x86_64-pc-windows-msvc.tar.gz",
        "a6ef3442cb12766a88b39311d79244289e4f9763e2c53ff4fbebc2cb653cc5f3", "codex.exe"),
}


def install_codex_package(data, target):
    archive_name, expected, binary_name = CODEX_PACKAGES[target]
    runtime = data / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    print("Downloading the official Codex desktop package…", flush=True)
    with tempfile.TemporaryDirectory(prefix=".codex-install-", dir=runtime) as temporary:
        temporary = Path(temporary)
        archive = temporary / archive_name
        download(f"https://github.com/openai/codex/releases/download/{CODEX_VERSION}/{archive_name}",
                 archive, 256 * 1024 * 1024)
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise RuntimeError("Codex download checksum mismatch. Start Shadow Aim again to retry.")
        staging = temporary / "package"
        staging.mkdir()
        with tarfile.open(archive, "r:gz") as package:
            total = 0
            for member in package:
                path = Path(member.name)
                if (path.is_absolute() or ".." in path.parts or "\\" in member.name
                        or ":" in member.name or not path.parts
                        or path.parts[0] not in {"bin", "codex-path", "codex-resources", "codex-package.json"}
                        or not (member.isfile() or member.isdir())):
                    raise RuntimeError("Unexpected path or file type in Codex package")
                total += member.size
                if total > 1024 * 1024 * 1024:
                    raise RuntimeError("Codex package exceeded its expected unpacked size")
                destination = staging / path
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with package.extractfile(member) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
                destination.chmod(0o755 if member.mode & 0o111 else 0o644)
        binary = staging / "bin" / binary_name
        if not binary.is_file():
            raise RuntimeError("Codex package is missing its launcher")
        check_home = temporary / "check-home"
        check_home.mkdir()
        environment = {**os.environ, "CODEX_HOME": str(check_home)}
        run([binary, "--version"], env=environment, timeout=30)
        run([binary, "app-server", "--help"], env=environment, timeout=30,
            stdout=subprocess.DEVNULL)
        # Publish the executable last. Interrupted setup remains detectable and
        # retries; none of the user's authentication or settings are touched.
        files = sorted((p for p in staging.rglob("*") if p.is_file()), key=lambda p: p == binary)
        for source in files:
            destination = runtime / source.relative_to(staging)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)


def desktop_shortcut(data):
    if os.name == "nt":
        programs = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Microsoft/Windows/Start Menu/Programs"
        programs.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="shadow-aim-shortcut-") as temporary:
            script = Path(temporary) / "shortcut.ps1"
            script.write_text('param([string]$Link,[string]$Target,[string]$Working)\n'
                '$shell = New-Object -ComObject WScript.Shell\n'
                '$shortcut = $shell.CreateShortcut($Link)\n'
                '$shortcut.TargetPath = $Target\n$shortcut.WorkingDirectory = $Working\n$shortcut.Save()\n')
            run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                 programs / "Shadow Aim.lnk", ROOT / "Start Shadow Aim.cmd", ROOT])
        print("Shadow Aim was added to the Windows Start menu.")
        return
    applications = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    # Desktop Exec is its own syntax, not shell syntax. Escape reserved characters
    # inside quoted arguments, and prevent '%' from introducing field codes.
    def quoted(value):
        text = str(value).replace("%", "%%")
        for character in ("\\", '"', "`", "$"):
            text = text.replace(character, "\\" + character)
        return '"' + text + '"'
    launcher = applications / "shadow-aim.desktop"
    if any(c in str(ROOT) + str(data) for c in "\n\r"):
        raise RuntimeError("Move the app to a folder without line breaks before installing its shortcut.")
    packaged = (ROOT / "Start Shadow Aim.sh").is_file()
    launch = quoted(ROOT / "Start Shadow Aim.sh") if packaged else " ".join((quoted(sys.executable), quoted(ROOT / "launch.py")))
    launcher.write_text("[Desktop Entry]\nType=Application\nName=Shadow Aim\n"
        "Comment=Native aim training with your ChatGPT coach\n"
        f"Exec=env {quoted('AIMCOACH_DATA_DIR=' + str(data))} {launch}\n"
        f"Path={ROOT}\nTerminal={str(packaged).lower()}\nCategories=Game;\n", encoding="utf-8")
    launcher.chmod(0o755)
    print(f"App-menu shortcut installed: {launcher}")


def check(data):
    engine = ROOT / "runtime" / ("godot.exe" if os.name == "nt" else "godot")
    candidates = [str(engine), os.environ.get("GODOT_BIN"), "/snap/godot-4/current/godot-4",
                  shutil.which("godot4"), shutil.which("godot")]
    found = next((p for p in candidates if p and Path(p).is_file()), None)
    print(f"Python: {platform.python_version()} ({sys.executable})")
    print(f"Game runtime: {found or 'MISSING: use the Linux release bundle or install Godot 4.7'}")
    search = os.pathsep.join((str(data / "runtime/bin"), str(Path.home() / ".local/bin"), os.environ.get("PATH", "")))
    print(f"Codex: {shutil.which('codex', path=search) or 'not installed; use --codex for AI coaching'}")
    managed = data / "runtime/venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    bundled = ROOT / "runtime/python/python.exe"
    audio_python = str(bundled) if bundled.is_file() and os.name == "nt" else str(managed) if managed.is_file() else sys.executable
    result = subprocess.run([audio_python, "-c", "from companion.voice_bridge import _load_media; _load_media()"],
                            cwd=ROOT, capture_output=True, text=True)
    print("Voice/music media: " + ("ready" if result.returncode == 0 else "not installed; use --audio"))
    print("ChatGPT/GPT-Live access is checked after sign-in in the game; installation does not guarantee account access.")
    return 0 if found and sys.version_info >= (3, 11) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", action="store_true", help="install pinned native voice/music dependencies in an app-owned venv")
    parser.add_argument("--codex", action="store_true", help="install official Codex privately if it is missing")
    parser.add_argument("--desktop", action="store_true", help="add the app to your Linux applications menu")
    parser.add_argument("--check", action="store_true", help="check dependencies without scanning files, signing in, or recording audio")
    args = parser.parse_args()
    if sys.version_info < (3, 11) or platform.system() not in ("Linux", "Windows"):
        parser.error("This release requires Linux or Windows and Python 3.11 or newer.")
    data = Path(os.environ.get("AIMCOACH_DATA_DIR", default_data_dir())).expanduser().resolve()
    try:
        if args.audio:
            install_audio(data)
        if args.codex:
            install_codex(data)
        if args.desktop:
            desktop_shortcut(data)
        return check(data)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
