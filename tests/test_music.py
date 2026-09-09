import json
from pathlib import Path
import struct
import tempfile
import threading
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


def make_tagged_flac(path, **tags):
    av = _av()
    source = make_wav(path.with_suffix(".fixture.wav"))
    try:
        with av.open(str(source), "r") as input_audio, av.open(str(path), "w") as output:
            output.metadata.update(tags)
            stream = output.add_stream("flac", rate=22050)
            stream.layout = "mono"
            for frame in input_audio.decode(input_audio.streams.audio[0]):
                for packet in stream.encode(frame):
                    output.mux(packet)
            for packet in stream.encode(None):
                output.mux(packet)
    finally:
        source.unlink()
    return path


class MusicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.root = self.directory / "picked-music"
        self.root.mkdir()
        self.library = MusicLibrary(self.directory / "appdata")
        self.addCleanup(self.library.close)

    def scan(self, root=None):
        self.library.start_scan(str(root or self.root))
        return self.wait_for_scan(self.library)

    def wait_for_scan(self, library):
        deadline = time.monotonic() + 10
        while library.progress()["status"] == "scanning" and time.monotonic() < deadline:
            time.sleep(.01)
        status = library.status()
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

    def test_interrupted_scan_saves_choice_and_checkpoints_then_resumes_cached_tracks(self):
        for filename in ("A.wav", "B.wav", "C.wav", "D.wav"):
            make_wav(self.root / filename)
        first_entered, first_release = threading.Event(), threading.Event()
        last_entered, last_release = threading.Event(), threading.Event()
        inspect = MusicLibrary._inspect

        def blocked_inspect(av, path, root, relative):
            if path.name == "A.wav":
                first_entered.set()
                self.assertTrue(first_release.wait(5), "first scan gate timed out")
            if path.name == "D.wav":
                last_entered.set()
                self.assertTrue(last_release.wait(5), "last scan gate timed out")
            return inspect(av, path, root, relative)

        catalog = self.directory / "appdata/music/library.json"
        try:
            with patch.object(self.library, "_inspect", side_effect=blocked_inspect), patch(
                    "companion.music.SCAN_CHECKPOINT_TRACKS", 2):
                self.library.start_scan(str(self.root))
                self.assertTrue(first_entered.wait(3))
                selected = json.loads(catalog.read_text())
                self.assertEqual(selected["root"], str(self.root))
                self.assertTrue(selected["scan_incomplete"])
                self.assertEqual(selected["tracks"], [])
                first_release.set()
                self.assertTrue(last_entered.wait(3))
                checkpoint = catalog.read_text()
                self.assertEqual(len(json.loads(checkpoint)["tracks"]), 3)
                with patch.object(self.library, "_sort_key", side_effect=AssertionError("progress must not sort")):
                    progress = self.library.progress()
                self.assertEqual(progress, {"folder_selected": True, "status": "scanning", "total": 3,
                                           "scanned": 3, "skipped": 0, "error": None, "complete": False})
                self.assertEqual(self.library.search("A")["scan"], progress)
                # Copy only durable bytes before close(), reproducing an abrupt exit.
                restored_catalog = self.directory / "restarted/music/library.json"
                restored_catalog.parent.mkdir(parents=True)
                restored_catalog.write_text(checkpoint)
                with patch.object(MusicLibrary, "_inspect", wraps=inspect) as inspected:
                    reopened = MusicLibrary(self.directory / "restarted")
                    self.addCleanup(reopened.close)
                    status = self.wait_for_scan(reopened)
                    self.assertEqual(status["total"], 4)
                    self.assertEqual([call.args[1].name for call in inspected.call_args_list], ["D.wav"])
                self.assertTrue(reopened.progress()["complete"])
                self.assertFalse(json.loads(restored_catalog.read_text())["scan_incomplete"])
        finally:
            first_release.set()
            last_release.set()
            self.library.close()

    def test_close_flushes_tracks_since_last_checkpoint(self):
        for filename in ("A.wav", "B.wav", "C.wav"):
            make_wav(self.root / filename)
        entered, release = threading.Event(), threading.Event()
        inspect = MusicLibrary._inspect

        def blocked_inspect(av, path, root, relative):
            if path.name == "C.wav":
                entered.set()
                release.wait(5)
            return inspect(av, path, root, relative)

        catalog = self.directory / "appdata/music/library.json"
        try:
            with patch.object(self.library, "_inspect", side_effect=blocked_inspect), patch(
                    "companion.music.SCAN_CHECKPOINT_SECONDS", 60):
                self.library.start_scan(str(self.root))
                self.assertTrue(entered.wait(3))
                self.assertEqual(len(json.loads(catalog.read_text())["tracks"]), 1)
                self.assertEqual(self.library.progress()["total"], 2)
                self.library.close()
                self.assertEqual(len(json.loads(catalog.read_text())["tracks"]), 2)
                self.assertTrue(json.loads(catalog.read_text())["scan_incomplete"])
        finally:
            release.set()
            for worker in self.library._workers:
                worker.join(timeout=2)

    def test_unavailable_saved_drive_preserves_folder_and_catalog(self):
        make_wav(self.root / "Saved.wav")
        self.scan()
        moved = self.root.with_name("disconnected")
        self.root.rename(moved)
        for incomplete in (False, True):
            with self.subTest(incomplete=incomplete):
                catalog = self.directory / "appdata/music/library.json"
                saved = json.loads(catalog.read_text())
                saved["scan_incomplete"] = incomplete
                catalog.write_text(json.dumps(saved))
                with patch("companion.music.os.walk", side_effect=AssertionError("drive is unavailable")):
                    reopened = MusicLibrary(self.directory / "appdata")
                    self.assertEqual(reopened.status()["root"], str(self.root))
                    self.assertEqual(reopened.progress()["total"], 1)
                    self.assertEqual(reopened.progress()["status"], "failed")
                    self.assertFalse(reopened.progress()["complete"])
                    self.assertIn("Reconnect", reopened.progress()["error"])
                    reopened.close()
                self.assertEqual(json.loads(catalog.read_text())["root"], str(self.root))
        moved.rename(self.root)
        reopened = MusicLibrary(self.directory / "appdata")
        self.addCleanup(reopened.close)
        self.assertEqual(self.wait_for_scan(reopened)["total"], 1)

    def test_unsaved_folder_never_starts_a_scan(self):
        with patch.object(self.library, "_save", side_effect=PermissionError("fixture")), patch(
                "companion.music.threading.Thread.start") as start:
            with self.assertRaisesRegex(MusicError, "could not be saved"):
                self.library.start_scan(str(self.root))
            start.assert_not_called()
            self.assertEqual(self.library.progress()["status"], "failed")

    def test_relaxing_selection_uses_labels_without_filling_with_unrelated_music(self):
        for filename in ("Chill/Ordinary title.wav", "Artist - Peaceful evening.wav",
                         "Loud rock.wav", "Sleepwalker.wav"):
            make_wav(self.root / filename)
        self.scan()
        selected = self.library.select_mood("relaxing", limit=1)
        self.assertEqual(selected["total"], 2)
        self.assertEqual(len(selected["tracks"]), 1)
        self.assertEqual(selected["scan"]["total"], 4)
        all_selected = self.library.select_mood("relaxing")["tracks"]
        self.assertEqual({track["title"] for track in all_selected}, {"Ordinary title", "Peaceful evening"})
        self.assertTrue(all("relative" not in track and "path" not in track for track in all_selected))
        for filename in (self.root / "Chill/Ordinary title.wav", self.root / "Artist - Peaceful evening.wav"):
            filename.unlink()
        self.scan()
        self.assertEqual(self.library.select_mood("relaxing")["tracks"], [])

    def test_genre_and_mood_mixes_use_real_tags_and_require_both_when_combined(self):
        for filename in ("Rock/Stone.wav", "Jazz Trio - Blue.wav", "A Pop Evening.wav", "Rockabye.wav"):
            make_wav(self.root / filename)
        make_tagged_flac(self.root / "one.flac", title="Anvil", genre="Heavy Metal", mood="Energetic")
        make_tagged_flac(self.root / "two.flac", title="Quick", genre="Hard Rock", mood="Upbeat")
        make_tagged_flac(self.root / "three.flac", title="Water", genre="Nu Jazz", mood="Calm")
        make_tagged_flac(self.root / "four.flac", title="Road", album="Country Classics")
        self.scan()
        for query, expected in (("rock", {"Stone", "Quick"}), ("metal", {"Anvil"}),
                                ("jazz", {"Blue", "Water"}), ("pop", {"A Pop Evening"}),
                                ("country", {"Road"}), ("energetic", {"Anvil", "Quick"}),
                                ("energetic rock", {"Quick"}), ("relaxing jazz", {"Water"})):
            with self.subTest(query=query):
                selected = self.library.select_mix(query)
                self.assertEqual({track["title"] for track in selected["tracks"]}, expected)
                self.assertEqual(selected["total"], len(expected))
                self.assertTrue(selected["scan"]["complete"])
                self.assertNotIn("needs_selection", selected)
        self.assertEqual(self.library.select_mix("energetic rock")["selection_label"], "Energetic rock music")
        self.assertEqual(self.library.select_mix("energetic jazz")["tracks"], [])
        self.assertEqual(self.library.select_mix("rokk")["tracks"], [])

    def test_category_aliases_keep_specific_genres_narrow_and_support_custom_labels(self):
        for folder, title in (("HipHop", "A"), ("Rhythm and Blues", "B"), ("Lo-Fi", "C"),
                              ("Techno", "D"), ("House", "E"), ("Drum & Bass", "F"), ("Vaporwave", "G")):
            make_wav(self.root / folder / (title + ".wav"))
        self.scan()
        for query, expected in (("play some hip-hop music please", {"A"}), ("rap", {"A"}),
                                ("r&b", {"B"}), ("lofi", {"C"}), ("edm", {"D", "E", "F"}),
                                ("house", {"E"}), ("dnb", {"F"}), ("vaporwave", {"G"})):
            with self.subTest(query=query):
                selected = self.library.select_mix(query, limit=1)
                self.assertEqual(selected["total"], len(expected))
                self.assertEqual(len(selected["tracks"]), 1)
                self.assertEqual({track["title"] for track in self.library.select_mix(query)["tracks"]}, expected)
                self.assertTrue(all("path" not in track and "relative" not in track for track in selected["tracks"]))
        for query in (None, "", " ", "!!!", "music", "play some music please", "x" * 201):
            with self.subTest(query=query), self.assertRaises(MusicError):
                self.library.select_mix(query)
        for limit in (True, 0, 101):
            with self.subTest(limit=limit), self.assertRaises(MusicError):
                self.library.select_mix("rock", limit)

    def test_song_continuation_prefers_album_then_artist_and_starts_with_requested_track(self):
        for filename, title, artist, album in (("one", "Z requested", "First artist", "Greatest Hits"),
                                               ("two", "A companion", "First artist", "Greatest Hits"),
                                               ("three", "Other album", "First artist", "Second album"),
                                               ("four", "Unrelated", "Other artist", "Greatest Hits"),
                                               ("five", "Solo", "Solo artist", "Solo album")):
            make_tagged_flac(self.root / (filename + ".flac"), title=title, artist=artist, album=album)
        tracks = {track["title"]: track for track in self.scan()["tracks"]}
        album = self.library.continuation(tracks["Z requested"]["id"])
        self.assertEqual([track["title"] for track in album["tracks"]], ["Z requested", "A companion"])
        self.assertEqual(album["selection_label"], "Album: Greatest Hits")
        artist = self.library.continuation(tracks["Other album"]["id"])
        self.assertEqual([track["title"] for track in artist["tracks"]], ["Other album", "A companion", "Z requested"])
        self.assertEqual(artist["selection_label"], "More from First artist")
        solo = self.library.continuation(tracks["Solo"]["id"])
        self.assertEqual([track["title"] for track in solo["tracks"]], ["Solo"])
        limited = self.library.continuation(tracks["Z requested"]["id"], limit=1)
        self.assertEqual(limited["tracks"], [tracks["Z requested"]])
        self.assertEqual(limited["total"], 2)
        self.assertTrue(limited["scan"]["complete"])

    def test_song_continuation_without_tags_stays_in_same_parent_folder(self):
        for filename in ("Chosen/Z song.wav", "Chosen/A song.wav", "Chosen/nested/Nested.wav", "Elsewhere/Other.wav"):
            make_wav(self.root / filename)
        tracks = {track["title"]: track for track in self.scan()["tracks"]}
        selected = self.library.continuation(tracks["Z song"]["id"])
        self.assertEqual([track["title"] for track in selected["tracks"]], ["Z song", "A song"])
        self.assertEqual(selected["selection_label"], "Same folder")
        self.assertEqual(self.library.continuation(tracks["Other"]["id"])["total"], 1)
        for identifier in (None, "not indexed", "0" * 24):
            with self.subTest(identifier=identifier), self.assertRaises(MusicError):
                self.library.continuation(identifier)
        for limit in (True, 0, 101):
            with self.subTest(limit=limit), self.assertRaises(MusicError):
                self.library.continuation(tracks["Z song"]["id"], limit)

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
                output.metadata.update(title="Tagged " + filename, artist="Tagged artist", album="Tagged album",
                                       genre="Ambient", mood="Relaxing")
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
        self.assertEqual(track["genre"], "Ambient")
        self.assertEqual(track["mood"], "Relaxing")
        self.assertEqual(self.library.select_mood("relaxing")["total"], 2)
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
