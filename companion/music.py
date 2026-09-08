"""An explicitly selected local music library; discovery never opens audio devices."""
from __future__ import annotations

from difflib import SequenceMatcher
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unicodedata
import wave

LOG = logging.getLogger("shadow_aim.music")
SUPPORTED_EXTENSIONS = frozenset({".mp3", ".ogg", ".oga", ".wav", ".flac", ".m4a", ".aac", ".opus", ".aif", ".aiff", ".wma"})
MAX_CACHE_BYTES = 1024 * 1024 * 1024
MAX_TRACK_BYTES = 512 * 1024 * 1024
MAX_CACHE_TRACKS = 16


class MusicError(ValueError):
    pass


def _av():
    # Reuse the optional native media installation without touching microphone,
    # speakers, voice authentication, or the user's Python environment.
    from .voice_bridge import _load_media
    try:
        return _load_media()[1]
    except RuntimeError as error:
        raise MusicError("Music support is not installed. Restart Shadow Aim after installing native audio support.") from error


def _normalized(value):
    text = unicodedata.normalize("NFKD", str(value)).casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.findall(r"\w+", text, flags=re.UNICODE))


def _public(track):
    return {key: track.get(key, "") for key in ("id", "title", "artist", "album")}


class MusicLibrary:
    def __init__(self, data_dir):
        self._directory = Path(data_dir).expanduser().resolve() / "music"
        self._catalog = self._directory / "library.json"
        self._cache = self._directory / "cache"
        self._lock = threading.RLock()
        self._prepare_lock = threading.Lock()
        self._workers = []
        self._generation = 0
        self._closed = False
        self._root = None
        self._tracks = {}
        self._state = "idle"
        self._error = None
        self._scanned = 0
        self._skipped = 0
        # Restore only our catalog. No traversal of the selected folder occurs.
        try:
            saved = json.loads(self._catalog.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                raise ValueError("Invalid saved library")
            if saved.get("version") == 1 and isinstance(saved.get("root"), str):
                root = Path(saved["root"])
                if not root.is_absolute():
                    raise ValueError("Invalid saved library root")
                tracks = saved.get("tracks", [])
                if not isinstance(tracks, list):
                    raise ValueError("Invalid saved music entries")
                for track in tracks:
                    if not isinstance(track, dict) or not all(isinstance(track.get(k), str) for k in
                            ("id", "relative", "title", "artist", "album", "codec")):
                        raise ValueError("Invalid saved music entry")
                    relative = Path(track["relative"])
                    if (relative.is_absolute() or ".." in relative.parts
                            or not re.fullmatch(r"[0-9a-f]{24}", track["id"])):
                        raise ValueError("Invalid saved music entry")
                self._root = root
                self._tracks = {t["id"]: t for t in tracks}
                self._state = "ready"
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError):
            self._error = "The saved music library could not be loaded. Choose your music folder again."
            LOG.warning("music_catalog_invalid")

    def status(self):
        with self._lock:
            tracks = sorted(self._tracks.values(), key=self._sort_key)
            return {"root": str(self._root) if self._root else None,
                    "status": self._state, "tracks": [_public(t) for t in tracks[:50]],
                    "total": len(tracks), "scanned": self._scanned,
                    "skipped": self._skipped, "error": self._error}

    @staticmethod
    def _sort_key(track):
        return (_normalized(track["title"]), _normalized(track.get("artist", "")), track["id"])

    def start_scan(self, folder):
        if not isinstance(folder, str) or not folder.strip() or len(folder) > 4096:
            raise MusicError("Choose a music folder first.")
        try:
            root = Path(folder).expanduser().resolve(strict=True)
            if not root.is_dir():
                raise MusicError("Choose a folder, not a music file.")
            # Opening the selected root checks actual access, including ACLs.
            with os.scandir(root):
                pass
        except (OSError, RuntimeError) as error:
            raise MusicError("That folder is unavailable or cannot be read. Choose an accessible music folder.") from error
        with self._lock:
            if self._closed:
                raise MusicError("The music library is shutting down.")
            self._generation += 1
            generation = self._generation
            self._root, self._tracks = root, {}
            self._state, self._error = "scanning", None
            self._scanned = self._skipped = 0
            worker = threading.Thread(target=self._scan, args=(root, generation), daemon=True,
                                      name="shadow-aim-music-scan")
            self._workers = [t for t in self._workers if t.is_alive()]
            self._workers.append(worker)
            worker.start()
            return self.status()

    def _active(self, generation):
        with self._lock:
            return not self._closed and generation == self._generation

    def _scan(self, root, generation):
        started = time.monotonic()
        try:
            av = _av()
            def unreadable(_error):
                with self._lock:
                    if self._active(generation):
                        self._skipped += 1
            for directory, folders, files in os.walk(root, followlinks=False, onerror=unreadable):
                if not self._active(generation):
                    return
                folders[:] = sorted(d for d in folders if not (Path(directory) / d).is_symlink())
                for filename in sorted(files):
                    if not self._active(generation):
                        return
                    path = Path(directory) / filename
                    if path.suffix.lower() not in SUPPORTED_EXTENSIONS or path.is_symlink():
                        continue
                    try:
                        relative = path.resolve(strict=True).relative_to(root)
                        track = self._inspect(av, path, root, relative)
                    except Exception as error:
                        with self._lock:
                            if self._active(generation):
                                self._skipped += 1
                        LOG.debug("music_file_skipped error_type=%s", type(error).__name__)
                    else:
                        with self._lock:
                            if self._active(generation):
                                self._tracks[track["id"]] = track
                    with self._lock:
                        if self._active(generation):
                            self._scanned += 1
            with self._lock:
                if not self._active(generation):
                    return
                self._save()
                self._state = "ready"
                LOG.info("music_scan_complete tracks=%s skipped=%s duration_ms=%s", len(self._tracks),
                         self._skipped, round((time.monotonic() - started) * 1000))
        except Exception as error:
            with self._lock:
                if self._active(generation):
                    self._state = "failed"
                    self._error = str(error) if isinstance(error, MusicError) else "The music folder could not be indexed. Check that the drive is still connected."
            LOG.warning("music_scan_failed duration_ms=%s", round((time.monotonic() - started) * 1000), exc_info=True)

    @staticmethod
    def _inspect(av, path, root, relative):
        with av.open(str(path), mode="r") as container:
            if not container.streams.audio:
                raise MusicError("No audio stream")
            stream = container.streams.audio[0]
            tags = {str(k).casefold(): str(v).strip()[:500] for k, v in
                    {**container.metadata, **stream.metadata}.items()}
            title, artist = path.stem, ""
            if " - " in title:
                artist, title = title.split(" - ", 1)
            identifier = hashlib.sha256((str(root) + "\0" + relative.as_posix()).encode()).hexdigest()[:24]
            return {"id": identifier, "relative": relative.as_posix(),
                    "title": tags.get("title") or title, "artist": tags.get("artist") or artist,
                    "album": tags.get("album", ""), "codec": stream.codec_context.name}

    def _save(self):
        self._directory.mkdir(parents=True, exist_ok=True)
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=self._directory, encoding="utf-8", delete=False) as file:
                temp_name = file.name
                json.dump({"version": 1, "root": str(self._root), "tracks": list(self._tracks.values())}, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_name, self._catalog)
        finally:
            if temp_name:
                Path(temp_name).unlink(missing_ok=True)

    def list_tracks(self, query="", limit=50):
        return self.search(query, limit)["tracks"]

    def search(self, query="", limit=50):
        if not isinstance(query, str) or len(query) > 500:
            raise MusicError("Use a song title or artist of at most 500 characters.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise MusicError("Music search limit must be between 1 and 100.")
        with self._lock:
            tracks = list(self._tracks.values())
        original = _normalized(query)
        if not original:
            tracks.sort(key=self._sort_key)
            return {"tracks": [_public(t) for t in tracks[:limit]], "total": len(tracks),
                    "exact_match_id": None, "ambiguous": False}
        cleaned = re.sub(r"^(?:please )?(?:play|put on|listen to)(?: the song)? ", "", original)
        cleaned = re.sub(r" please$", "", cleaned)
        queries = {original, cleaned}
        ranked = []
        exact = []
        for track in tracks:
            title, artist, album = (_normalized(track.get(k, "")) for k in ("title", "artist", "album"))
            names = {title, f"{title} {artist}".strip(), f"{artist} {title}".strip(),
                     f"{title} by {artist}".strip()}
            if queries & names:
                score = 1.0
                exact.append(track["id"])
            else:
                words = set(f"{title} {artist} {album}".split())
                score = 0.0
                for text in queries:
                    query_words = set(text.split()) - {"by"}
                    overlap = len(words & query_words) / max(1, len(query_words))
                    fuzzy = max(SequenceMatcher(None, text, name).ratio() for name in names)
                    score = max(score, .90 * overlap if overlap == 1 else .65 * overlap, .85 * fuzzy)
            if score >= .55:
                ranked.append((score, track))
        ranked.sort(key=lambda row: (-row[0], self._sort_key(row[1])))
        # Fuzzy matches are suggestions. Only a unique exact title/artist match
        # authorizes voice callers to select a song without clarification.
        return {"tracks": [{**_public(t), "score": round(score, 3)} for score, t in ranked[:limit]],
                "total": len(ranked), "exact_match_id": exact[0] if len(exact) == 1 else None,
                "ambiguous": len(exact) > 1 or (not exact and len(ranked) > 1)}

    def _source(self, track_id):
        if not isinstance(track_id, str) or not re.fullmatch(r"[0-9a-f]{24}", track_id):
            raise MusicError("Choose a song from your music library.")
        with self._lock:
            root, track = self._root, self._tracks.get(track_id)
            if root is None or track is None:
                raise MusicError("That song is no longer in the selected music folder. Search again.")
            track = dict(track)
        try:
            # Resolve again on every play, including cache hits. A moved file or
            # swapped symlink must not escape the folder the player selected.
            path = (root / track["relative"]).resolve(strict=True)
            path.relative_to(root)
            if not path.is_file():
                raise ValueError("not a file")
        except (OSError, RuntimeError, ValueError) as error:
            raise MusicError("That song is unavailable or outside your selected music folder. Choose the folder again.") from error
        return path, track

    def get_track(self, track_id):
        """Validate a selection and expose metadata without decoding the song."""
        _source, track = self._source(track_id)
        return _public(track)

    def prepare(self, track_id):
        source, track = self._source(track_id)
        codec = track.get("codec", "")
        extension = source.suffix.lower()
        if extension == ".mp3" and codec.startswith("mp3"):
            return {"track": _public(track), "path": str(source), "format": "mp3"}
        if extension in {".ogg", ".oga"} and codec == "vorbis":
            return {"track": _public(track), "path": str(source), "format": "ogg"}
        if extension == ".wav" and codec in {"pcm_s16le", "pcm_u8"}:
            return {"track": _public(track), "path": str(source), "format": "wav"}
        with self._prepare_lock:
            source, track = self._source(track_id)
            stat = source.stat()
            key = hashlib.sha256(f"{track_id}:{stat.st_mtime_ns}:{stat.st_size}".encode()).hexdigest()
            target = self._cache / (key + ".wav")
            self._cache.mkdir(parents=True, exist_ok=True)
            if not target.is_file():
                self._transcode(source, target, track_id)
            os.utime(target, None)
            self._prune_cache(target)
        return {"track": _public(track), "path": str(target), "format": "wav"}

    @staticmethod
    def _transcode(source, target, track_id):
        started = time.monotonic()
        temporary = None
        try:
            av = _av()
            with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".wav", delete=False) as file:
                temporary = Path(file.name)
            written = 0
            with av.open(str(source), mode="r") as audio, wave.open(str(temporary), "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(44100)
                resampler = av.AudioResampler(format="s16", layout="stereo", rate=44100)
                def write_frames(frames):
                    nonlocal written
                    for frame in frames:
                        count = frame.samples * 4
                        written += count
                        if written > MAX_TRACK_BYTES:
                            raise MusicError("This song is too long to prepare for playback. Choose a shorter track.")
                        output.writeframesraw(bytes(frame.planes[0])[:count])
                for frame in audio.decode(audio.streams.audio[0]):
                    write_frames(resampler.resample(frame))
                write_frames(resampler.resample(None))
                if not written:
                    raise MusicError("That file contains no playable audio.")
            os.replace(temporary, target)
            LOG.info("music_prepared track_id=%s duration_ms=%s bytes=%s", track_id,
                     round((time.monotonic() - started) * 1000), written)
        except Exception as error:
            LOG.warning("music_prepare_failed track_id=%s duration_ms=%s", track_id,
                        round((time.monotonic() - started) * 1000), exc_info=True)
            if isinstance(error, MusicError):
                raise
            raise MusicError("That song could not be decoded. Choose another track or check the file.") from error
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def _prune_cache(self, keep):
        files = sorted((p for p in self._cache.glob("*.wav") if p != keep), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files) + keep.stat().st_size
        while files and (len(files) + 1 > MAX_CACHE_TRACKS or total > MAX_CACHE_BYTES):
            oldest = files.pop(0)
            total -= oldest.stat().st_size
            oldest.unlink(missing_ok=True)

    def close(self):
        with self._lock:
            self._closed = True
            self._generation += 1
            workers = list(self._workers)
        for worker in workers:
            worker.join(timeout=1)
