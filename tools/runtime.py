"""Download the pinned, standard Godot runtimes used by desktop release bundles."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import urllib.request
import zipfile

GODOT_VERSION = "4.7-stable"
RUNTIMES = {
    "linux-x86_64": ("Godot_v4.7-stable_linux.x86_64.zip", "godot",
        "b639ca9c1ddea39bb3df89bd5283a51ca6047467abe6b25e9436566f2b2082ede633025073989ecf39c7d5d3c2493d80ea13e3af6dd5e261bbf89e462d6d2214"),
    "windows-x86_64": ("Godot_v4.7-stable_win64.exe.zip", "godot.exe",
        "41645a908eb3181d6f2d1201ed7b6d6f095f6a23aaed8903d5d255277cc8d142814f3e6817f865b3cac142c39b8aff99280091d3bbdaa301517730b3ba0522b9"),
}
PYTHON_ARCHIVE = "python-3.12.10-embed-amd64.zip"
PYTHON_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"


def default_data_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Shadow Aim"
    return Path.home() / ".local/share/aimcoach"


def default_state_dir() -> Path:
    return default_data_dir() / "logs" if os.name == "nt" else Path.home() / ".local/state/aimcoach"


def download(url: str, destination: Path, max_bytes: int) -> None:
    temporary = destination.with_suffix(destination.suffix + ".part")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=45) as response, temporary.open("wb") as output:
            copied = 0
            while chunk := response.read(1024 * 1024):
                copied += len(chunk)
                if copied > max_bytes:
                    raise RuntimeError("Download exceeded its expected size limit")
                output.write(chunk)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def godot_runtime(cache: Path, target: str = "linux-x86_64") -> Path:
    archive_name, binary_name, expected = RUNTIMES[target]
    cache = cache / target
    archive = cache / archive_name
    cache.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        print(f"Downloading the standard Godot 4.7 runtime for {target}…", flush=True)
        download(f"https://github.com/godotengine/godot-builds/releases/download/{GODOT_VERSION}/{archive_name}", archive, 120 * 1024 * 1024)
    with archive.open("rb") as file:
        actual = hashlib.file_digest(file, "sha512").hexdigest()
    if actual != expected:
        archive.unlink(missing_ok=True)
        raise RuntimeError("Godot download checksum did not match the official release. Retry the build.")
    binary = cache / binary_name
    with zipfile.ZipFile(archive) as zipped:
        with zipped.open(archive_name.removesuffix(".zip")) as source, binary.open("wb") as output:
            shutil.copyfileobj(source, output)
    binary.chmod(0o755)
    return binary


def godot_notices(destination: Path) -> None:
    for name in ("LICENSE.txt", "COPYRIGHT.txt"):
        download(f"https://raw.githubusercontent.com/godotengine/godot/{GODOT_VERSION}/{name}",
                 destination / ("GODOT_" + name), 1024 * 1024)


def windows_python(cache: Path, destination: Path) -> None:
    """Bundle CPython; optional codec wheels are installed by the user, not redistributed."""
    archive = cache / PYTHON_ARCHIVE
    if not archive.is_file():
        print("Downloading the official Windows Python runtime…", flush=True)
        download(f"https://www.python.org/ftp/python/3.12.10/{PYTHON_ARCHIVE}", archive, 20 * 1024 * 1024)
    with archive.open("rb") as file:
        if hashlib.file_digest(file, "sha256").hexdigest() != PYTHON_SHA256:
            archive.unlink(missing_ok=True)
            raise RuntimeError("Windows Python checksum mismatch; retry the build.")
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zipped:
        _extract_flat(zipped, destination)
    # Explicit isolated import paths: zip stdlib, interpreter DLLs, this app, and
    # bundled wheels. Neither PYTHONPATH nor the user's Python install is needed.
    (destination / "python312._pth").write_text("python312.zip\n.\n../..\nLib/site-packages\nimport site\n")


def install_windows_audio(destination: Path, manifest_path: Path) -> None:
    """Install a fixed set of hash-checked PyPI wheels using embedded Python, without pip."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    marker = destination / "shadow-aim-audio.json"
    if marker.is_file() and json.loads(marker.read_text()) == manifest:
        print("Native voice and music support is already installed.")
        return
    if destination.exists():
        raise RuntimeError("An incomplete or older audio installation exists. Extract a fresh release folder and run setup again.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".audio-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "packages"
        staging.mkdir()
        for item in manifest["wheels"]:
            if not item["url"].startswith("https://files.pythonhosted.org/") or Path(item["filename"]).name != item["filename"]:
                raise RuntimeError("Invalid audio package source")
            print(f"Installing {item['package']} {item['version']}…", flush=True)
            wheel = Path(temporary) / item["filename"]
            download(item["url"], wheel, 120 * 1024 * 1024)
            with wheel.open("rb") as file:
                if hashlib.file_digest(file, "sha256").hexdigest() != item["sha256"]:
                    raise RuntimeError("Audio package checksum mismatch. Run setup again.")
            with zipfile.ZipFile(wheel) as zipped:
                for entry in zipped.infolist():
                    path = Path(entry.filename)
                    if path.is_absolute() or ".." in path.parts or "\\" in entry.filename:
                        raise RuntimeError("Invalid path in audio package")
                    if entry.is_dir():
                        continue
                    if any(part.endswith(".data") for part in path.parts):
                        # Optional command-line entry points are not used by the app.
                        continue
                    target = staging / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zipped.open(entry) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
        (staging / marker.name).write_text(json.dumps(manifest))
        os.replace(staging, destination)


def _extract_flat(zipped, destination):
    for entry in zipped.infolist():
        name = Path(entry.filename)
        if name.is_absolute() or len(name.parts) != 1 or entry.is_dir():
            raise RuntimeError("Unexpected path in official embedded Python archive")
        with zipped.open(entry) as source, (destination / name).open("wb") as output:
            shutil.copyfileobj(source, output)
