import json
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

from companion.music import MusicError, MusicLibrary, _av


def make_wav(path, frames=2205):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(22050)
        audio.writeframes(b"".join(struct.pack("<h", (i % 200) * 100 - 10000) for i in range(frames)))
    return path


class MusicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.root = self.directory / "picked-music"
        self.root.mkdir()
        self.library = MusicLibrary(self.directory / "appdata")
        self.addCleanup(self.library.close)

    def scan(self, root=None):
        self.library.start_scan(str(root or self.root))
        deadline = time.monotonic() + 10
        while self.library.status()["status"] == "scanning" and time.monotonic() < deadline:
            time.sleep(.01)
        status = self.library.status()
        self.assertEqual(status["status"], "ready", status)
        return status

    def test_constructor_is_inert_and_folder_must_be_selected(self):
        with patch("companion.music.os.walk", side_effect=AssertionError("must not scan")):
            library = MusicLibrary(self.directory / "another-appdata")
            self.addCleanup(library.close)
            self.assertIsNone(library.status()["root"])
            self.assertEqual(library.status()["status"], "idle")
        for folder in (None, "", str(self.root / "missing")):
            with self.subTest(folder=folder), self.assertRaises(MusicError):
                self.library.start_scan(folder)
        with self.assertRaises(MusicError):
            self.library.start_scan(str(make_wav(self.root / "file.wav")))

    def test_recursive_scan_skips_symlinks_bad_media_and_unrelated_files(self):
        make_wav(self.root / "Alpha - First.wav")
        make_wav(self.root / "album" / "Beta - Second.WAV")
        (self.root / "broken.mp3").write_bytes(b"not audio")
        (self.root / "notes.txt").write_text("not music")
        outside = self.directory / "private"
        make_wav(outside / "Never indexed.wav")
        (self.root / "linked-folder").symlink_to(outside, target_is_directory=True)
        (self.root / "linked.wav").symlink_to(outside / "Never indexed.wav")
        status = self.scan()
        self.assertEqual(status["total"], 2)
        self.assertEqual(status["skipped"], 1)
        self.assertEqual(status["scanned"], 3)
        self.assertEqual([t["title"] for t in status["tracks"]], ["First", "Second"])
        self.assertNotIn("path", status["tracks"][0])
        self.assertNotIn("relative", status["tracks"][0])

    def test_catalog_and_stable_ids_survive_restart_without_scanning(self):
        make_wav(self.root / "nested" / "A - Saved.wav")
        first = self.scan()["tracks"]
        with patch("companion.music.os.walk", side_effect=AssertionError("must not scan")):
            reopened = MusicLibrary(self.directory / "appdata")
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.status()["tracks"], first)
            self.assertEqual(reopened.status()["root"], str(self.root))
        self.assertEqual(self.scan()["tracks"], first)

    def test_natural_title_artist_query_and_ambiguous_titles(self):
        for filename in ("Artist One - Déjà Vu.wav", "Artist Two - Déjà Vu.wav", "A - Different.wav"):
            make_wav(self.root / filename)
        self.scan()
        duplicate = self.library.search("please play deja vu")
        self.assertTrue(duplicate["ambiguous"])
        self.assertIsNone(duplicate["exact_match_id"])
        selected = self.library.search("play deja vu by artist two please")
        self.assertFalse(selected["ambiguous"])
        self.assertEqual(selected["tracks"][0]["artist"], "Artist Two")
        self.assertEqual(selected["exact_match_id"], selected["tracks"][0]["id"])
        fuzzy = self.library.search("diferent")
        self.assertEqual(fuzzy["tracks"][0]["title"], "Different")
        self.assertIsNone(fuzzy["exact_match_id"])
        self.assertEqual(self.library.search("completely missing unrelated song")["tracks"], [])
        self.assertEqual(len(self.library.list_tracks(limit=1)), 1)

    def test_prepare_native_wav_is_deterministic_and_revalidates_indexed_source(self):
        source = make_wav(self.root / "Native.wav")
        identifier = self.scan()["tracks"][0]["id"]
        result = self.library.prepare(identifier)
        self.assertEqual(result, self.library.prepare(identifier))
        self.assertEqual(result["path"], str(source))
        self.assertEqual(result["format"], "wav")
        self.assertFalse((self.directory / "appdata/music/cache").exists())
        source.unlink()
        with self.assertRaises(MusicError):
            self.library.prepare(identifier)

    def test_prepare_rejects_path_escape_and_unknown_id(self):
        source = make_wav(self.root / "Indexed.wav")
        outside = make_wav(self.directory / "Outside.wav")
        identifier = self.scan()["tracks"][0]["id"]
        for invalid in ("../Outside.wav", str(outside), "a" * 24, None, True):
            with self.subTest(invalid=invalid), self.assertRaises(MusicError):
                self.library.prepare(invalid)
        source.unlink()
        source.symlink_to(outside)
        with self.assertRaises(MusicError):
            self.library.prepare(identifier)

    def test_catalog_cannot_inject_relative_escape(self):
        make_wav(self.root / "Safe.wav")
        self.scan()
        catalog = self.directory / "appdata/music/library.json"
        saved = json.loads(catalog.read_text())
        saved["tracks"][0]["relative"] = "../Outside.wav"
        catalog.write_text(json.dumps(saved))
        reopened = MusicLibrary(self.directory / "appdata")
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.status()["total"], 0)
        self.assertIn("Choose", reopened.status()["error"])

    def test_flac_metadata_and_selected_only_conversion_uses_cached_pcm16(self):
        av = _av()
        source = make_wav(self.directory / "source.wav")
        for filename in ("one.flac", "two.flac"):
            with av.open(str(source), "r") as input_audio, av.open(str(self.root / filename), "w") as output:
                output.metadata.update(title="Tagged " + filename, artist="Tagged artist", album="Tagged album")
                stream = output.add_stream("flac", rate=22050)
                stream.layout = "mono"
                for frame in input_audio.decode(input_audio.streams.audio[0]):
                    for packet in stream.encode(frame):
                        output.mux(packet)
                for packet in stream.encode(None):
                    output.mux(packet)
        status = self.scan()
        self.assertEqual(status["total"], 2)
        cache = self.directory / "appdata/music/cache"
        self.assertFalse(cache.exists())
        track = status["tracks"][0]
        self.assertEqual(track["artist"], "Tagged artist")
        self.assertEqual(track["album"], "Tagged album")
        prepared = self.library.prepare(track["id"])
        self.assertEqual(prepared["format"], "wav")
        with wave.open(prepared["path"]) as converted:
            self.assertEqual(converted.getsampwidth(), 2)
            self.assertEqual(converted.getnchannels(), 2)
            self.assertEqual(converted.getframerate(), 44100)
            self.assertGreater(converted.getnframes(), 0)
        with patch.object(self.library, "_transcode", side_effect=AssertionError("must reuse cache")):
            self.assertEqual(self.library.prepare(track["id"]), prepared)
        self.assertEqual(len(list(cache.glob("*.wav"))), 1)
        # A cached decode never permits a now-missing source.
        (self.root / "one.flac").unlink()
        with self.assertRaises(MusicError):
            self.library.prepare(track["id"])

    def test_cache_prunes_old_entries_while_keeping_selected_track(self):
        cache = self.directory / "appdata/music/cache"
        cache.mkdir(parents=True)
        old = make_wav(cache / "old.wav")
        keep = make_wav(cache / "current.wav")
        with patch("companion.music.MAX_CACHE_TRACKS", 1):
            self.library._prune_cache(keep)
        self.assertTrue(keep.exists())
        self.assertFalse(old.exists())


if __name__ == "__main__":
    unittest.main()
