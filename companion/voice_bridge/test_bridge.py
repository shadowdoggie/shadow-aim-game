import asyncio
import base64
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from companion.voice_bridge import APP_TOOL_NAMESPACE, VoiceBridge, VoiceError, _Session, _app_arguments


def tool_request(name, arguments, call_id="call-1"):
    return {"threadId": "voice-thread", "turnId": "turn-1", "callId": call_id,
            "namespace": APP_TOOL_NAMESPACE, "tool": name, "arguments": arguments}


class VoiceBridgeTests(unittest.TestCase):
    def test_disabled_voice_does_not_open_worker_or_media(self):
        voice = VoiceBridge()
        self.assertEqual(voice.status()["state"], "off")
        self.assertIsNone(voice._loop)
        self.assertFalse(voice.push_audio(base64.b64encode(bytes(960)).decode())["accepted"])
        self.assertIsNone(voice._session)

    def test_audio_rejects_wrong_format_and_unbounded_input(self):
        voice = VoiceBridge()
        for audio, rate, channels in [("!", 24000, 1), ("AA==", 24000, 1),
                                      ("", 16000, 1), ("", 24000, 2),
                                      (base64.b64encode(bytes(12002)).decode(), 24000, 1)]:
            with self.subTest(rate=rate, channels=channels, length=len(audio)):
                with self.assertRaises(VoiceError):
                    voice.push_audio(audio, rate, channels)

    def test_event_gap_is_explicit_and_stop_discards_queued_audio(self):
        voice = VoiceBridge()
        for _ in range(501):
            voice._emit({"type": "audio", "audio": "AAAA"})
        self.assertTrue(voice.events(0)["gap"])
        cursor = voice.status()["latest_seq"]
        voice.stop()
        events = voice.events(cursor)["events"]
        self.assertEqual([event["type"] for event in events], ["state"])
        self.assertEqual(events[0]["state"], "off")

    def test_stop_then_shutdown_waits_for_native_media_cleanup(self):
        voice = VoiceBridge()
        voice._ensure_loop()
        completed = threading.Event()

        class Session:
            async def close(self):
                await asyncio.sleep(.02)
                completed.set()

        voice._session = Session()
        voice._set_state("connected")
        voice.stop()
        voice.close()
        self.assertTrue(completed.is_set())
        self.assertFalse(voice._worker.is_alive())

    def test_superseded_session_cannot_emit_audio(self):
        async def scenario():
            voice = VoiceBridge()
            session = _Session(voice, voice._generation)
            voice.stop()
            cursor = voice.status()["latest_seq"]
            session.emit({"type": "audio", "audio": "AAAA"})
            self.assertEqual(voice.events(cursor)["events"], [])
        asyncio.run(scenario())

    def test_coaching_context_reaches_backing_model_before_voice_speaks(self):
        async def scenario():
            voice = VoiceBridge()
            session = _Session(voice, voice._generation)
            session.thread_id = "test-thread"
            calls = []

            async def rpc(method, params):
                calls.append((method, params))
                await asyncio.sleep(.01)
                return {}

            session.rpc = rpc
            context = {"accuracy_pct": 97}
            await asyncio.gather(session.update_context(context),
                                 session.update_context("Accuracy improved to 97 percent.", True))
            await session.announcement_task
            self.assertEqual([method for method, _ in calls],
                ["thread/inject_items", "thread/realtime/appendText", "thread/realtime/appendSpeech"])
            backing_text = calls[0][1]["items"][0]["content"][0]["text"]
            self.assertEqual(backing_text, calls[1][1]["text"])
            await session.update_context(context)
            self.assertEqual(len(calls), 3, "Repeated context must not grow the backing history")
        asyncio.run(scenario())

    def test_announcement_waits_for_user_and_model_turns_to_finish(self):
        async def scenario():
            session = _Session(VoiceBridge(), 0)
            calls = []

            async def rpc(method, params):
                calls.append(method)
                return {}

            session.rpc = rpc
            session.media_event({"type": "turn.created", "turn": {"id": "user-1", "role": "user"}})
            await session.update_context("Your accuracy improved.", True)
            await asyncio.sleep(.01)
            self.assertEqual(calls, [])
            session.media_event({"type": "turn.created", "turn": {"id": "reply", "role": "assistant"}})
            session.media_event({"type": "turn.done", "turn": {"id": "user-1", "role": "user"}})
            await asyncio.sleep(.01)
            self.assertEqual(calls, [], "An announcement must not collide with the answer either")
            session.media_event({"type": "turn.done", "turn": {"id": "reply", "role": "assistant"}})
            await asyncio.wait_for(session.announcement_task, 2)
            self.assertEqual(calls, ["thread/realtime/appendSpeech"])
        asyncio.run(scenario())

    def test_gameplay_suppresses_app_announcements_without_suppressing_conversation(self):
        async def scenario():
            voice = VoiceBridge()
            session = _Session(voice, 0)
            session.playing = True
            await session.update_context("An unsolicited practice cue.", True)
            self.assertIsNone(session.pending_announcement)
            self.assertIsNone(session.announcement_task)
            session.media_event({"type": "input_transcript.added", "item": {"text": "Can you explain?"}})
            session.media_event({"type": "output_transcript.added", "item": {"text": "Yes."}})
            events = voice.events()["events"]
            self.assertEqual([event["role"] for event in events], ["user", "assistant"])
        asyncio.run(scenario())

    def test_new_game_context_discards_an_outdated_waiting_announcement(self):
        async def scenario():
            session = _Session(VoiceBridge(), 0)
            calls = []

            async def rpc(method, params):
                calls.append(method)
                return {}

            session.rpc = rpc
            session.media_event({"type": "input_transcript.added", "item": {"text": "Wait"}})
            await session.update_context("Old result announcement.", True)
            await session.update_context({"playing": True, "drill": "tracking"})
            await session.announcement_task
            self.assertIsNone(session.pending_announcement)
            self.assertNotIn("thread/realtime/appendSpeech", calls)
        asyncio.run(scenario())

    def test_unfinished_user_turn_does_not_queue_a_surprise_announcement_indefinitely(self):
        async def scenario():
            session = _Session(VoiceBridge(), 0)
            session.active_turns["user"] = "user"
            await session.update_context("Old announcement.", True)
            session.pending_announcement = ("Old announcement.", time.monotonic() - 21)
            await session.announcement_task
            self.assertIsNone(session.pending_announcement)
        asyncio.run(scenario())

    def test_live_updates_only_inject_latest_snapshot_when_the_player_speaks(self):
        async def scenario():
            session = _Session(VoiceBridge(), 0)
            calls = []

            async def rpc(method, params):
                calls.append((method, params))
                return {}

            session.rpc = rpc
            for elapsed in (5, 10, 15):
                await session.update_live_state({"playing": True, "drill": "clicking",
                    "live_measurements": {"elapsed_s": elapsed, "remaining_s": 45 - elapsed,
                        "hits": 9, "shots": 10, "accuracy_pct": 90, "tracking_on_target_pct": None,
                        "snapshot_unix_s": 1788910000 + elapsed}})
            self.assertEqual([method for method, _ in calls], ["thread/realtime/appendText"] * 3)
            session.media_event({"type": "turn.created", "turn": {"id": "turn-question", "role": "user",
                "start_ms": 15200, "end_ms": 15400, "transcript": "How is my accuracy?"}})
            await asyncio.gather(*list(session.tasks))
            session.media_event({"type": "turn.done", "turn": {"id": "turn-question", "role": "user",
                "start_ms": 15200, "end_ms": 16600, "transcript": "How is my accuracy?"}})
            await asyncio.gather(*list(session.tasks))
            injections = [params for method, params in calls if method == "thread/inject_items"]
            self.assertEqual(len(injections), 1, "Repeated turn events must not duplicate telemetry history")
            text = injections[0]["items"][0]["content"][0]["text"]
            state = json.loads(text.split("\n", 1)[1])
            self.assertEqual(state["live_measurements"]["elapsed_s"], 15)
            self.assertIn("snapshot_age_s", state)
            self.assertNotIn("thread/realtime/appendSpeech", [method for method, _ in calls])
        asyncio.run(scenario())

    def test_completed_round_context_prevents_reinjecting_stale_live_measurements(self):
        async def scenario():
            session = _Session(VoiceBridge(), 0)
            calls = []

            async def rpc(method, params):
                calls.append(method)
                return {}

            session.rpc = rpc
            await session.update_live_state({"playing": True, "live_measurements": {"hits": 3, "shots": 5}})
            await session.update_context({"playing": False, "phase": "results", "report": {"hits": 30, "shots": 40}})
            before = len(calls)
            session.media_event({"type": "turn.created", "turn": {"id": "later", "role": "user"}})
            await asyncio.gather(*list(session.tasks))
            self.assertEqual(len(calls), before)
        asyncio.run(scenario())

    def test_app_controls_reject_paths_unknown_actions_and_invalid_volume(self):
        invalid = [
            ("get_game_state", {"path": "/tmp/song"}),
            ("run_shell", {"command": "play music"}),
            ("music_control", {"action": "play", "track_id": "../../music/song.mp3"}),
            ("music_control", {"action": "play", "query": "song", "track_id": "song"}),
            ("music_control", {"action": "pause", "query": "song"}),
            ("music_control", {"action": "play", "query": "   "}),
            ("set_audio_volume", {"channel": "microphone", "operation": "set", "value": 20}),
            ("set_audio_volume", {"channel": "voice", "operation": "set", "value": True}),
            ("set_audio_volume", {"channel": "music", "operation": "set", "value": float("nan")}),
            ("set_audio_volume", {"channel": "game", "operation": "increase", "value": 201}),
        ]
        for name, arguments in invalid:
            with self.subTest(name=name, arguments=arguments):
                with self.assertRaises(VoiceError):
                    _app_arguments(name, arguments)
        self.assertEqual(_app_arguments("music_control", {"action": "play", "query": "  artist  "}),
                         {"action": "play", "query": "artist"})

    def test_control_completion_waits_for_ack_and_duplicate_call_does_not_repeat_change(self):
        async def scenario():
            ack = threading.Event()
            entered = threading.Event()
            executed = []

            def handler(name, arguments):
                executed.append((name, arguments))
                entered.set()
                ack.wait(2)
                return {"status": "completed", "volume": 40}

            session = _Session(VoiceBridge(action_handler=handler), 0)
            session.thread_id = "voice-thread"
            replies = []

            async def send(value):
                replies.append(value)

            session.send = send
            request = tool_request("set_audio_volume", {"channel": "music", "operation": "decrease", "value": 10})
            first = asyncio.create_task(session.handle_app_action("request-1", request))
            second = asyncio.create_task(session.handle_app_action("request-2", request))
            try:
                while not entered.is_set():
                    await asyncio.sleep(.001)
                self.assertEqual(replies, [], "Queued native work is not a completed action")
            finally:
                ack.set()
            await asyncio.gather(first, second)
            self.assertEqual(len(executed), 1)
            self.assertEqual(len(replies), 2)
            self.assertTrue(all(reply["result"]["success"] for reply in replies))
            self.assertEqual(json.loads(replies[0]["result"]["contentItems"][0]["text"])["volume"], 40)
        asyncio.run(scenario())

    def test_wrong_session_and_ambiguous_or_unconfirmed_results_remain_honest(self):
        async def scenario():
            executions = []

            def handler(name, arguments):
                executions.append(name)
                return {"status": "needs_selection", "candidates": [{"id": "abc123", "title": "A song"}]}

            session = _Session(VoiceBridge(action_handler=handler), 0)
            session.thread_id = "voice-thread"
            session.send = AsyncMock()
            wrong = {**tool_request("get_game_state", {}), "threadId": "other-thread"}
            await session.handle_app_action("wrong", wrong)
            self.assertEqual(executions, [])
            self.assertFalse(session.send.call_args.args[0]["result"]["success"])
            await session.handle_app_action("selection", tool_request("music_control", {"action": "play", "query": "song"}))
            result = session.send.call_args.args[0]["result"]
            self.assertTrue(result["success"])
            self.assertEqual(json.loads(result["contentItems"][0]["text"])["status"], "needs_selection")
            session.bridge.action_handler = lambda *args: {"status": "queued"}
            await session.handle_app_action("queued", tool_request("music_control", {"action": "pause"}, "call-2"))
            self.assertFalse(session.send.call_args.args[0]["result"]["success"])
        asyncio.run(scenario())

    def test_native_ack_wait_does_not_block_the_codex_protocol_reader(self):
        async def scenario():
            ack = threading.Event()
            replied = asyncio.Event()

            def handler(name, arguments):
                ack.wait(2)
                return {"status": "completed"}

            session = _Session(VoiceBridge(action_handler=handler), 0)
            session.thread_id = "voice-thread"
            stream = asyncio.StreamReader()
            session.process = SimpleNamespace(stdout=stream)

            async def send(value):
                if value.get("id") == "control-request":
                    replied.set()

            session.send = send
            response = asyncio.get_running_loop().create_future()
            session.pending[99] = response
            reader = asyncio.create_task(session.read())
            stream.feed_data((json.dumps({"id": "control-request", "method": "item/tool/call",
                "params": tool_request("music_control", {"action": "pause"})}) + "\n" +
                json.dumps({"id": 99, "result": {"track": "Björk — Jóga"}}, ensure_ascii=False) + "\n").encode("utf-8"))
            try:
                self.assertEqual((await asyncio.wait_for(response, 1))["result"], {"track": "Björk — Jóga"})
                self.assertFalse(replied.is_set())
                ack.set()
                await asyncio.wait_for(replied.wait(), 1)
            finally:
                ack.set()
                session.closed = True
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
        asyncio.run(scenario())

    def test_music_stop_can_cancel_while_an_earlier_play_is_still_preparing(self):
        async def scenario():
            stopped = threading.Event()
            preparing = threading.Event()

            def handler(name, arguments):
                if arguments["action"] == "play":
                    preparing.set()
                    stopped.wait(2)
                    return {"status": "failed", "error": "Superseded by stop"}
                stopped.set()
                return {"status": "completed"}

            session = _Session(VoiceBridge(action_handler=handler), 0)
            play = asyncio.create_task(session.run_app_action("play", "music_control", {"action": "play", "query": "song"}))
            while not preparing.is_set():
                await asyncio.sleep(.001)
            stop = await asyncio.wait_for(session.run_app_action("stop", "music_control", {"action": "stop"}), 1)
            self.assertEqual(stop["status"], "completed")
            self.assertEqual((await play)["status"], "failed")
        asyncio.run(scenario())

    def test_selected_app_auth_environment_is_used_without_api_keys(self):
        async def scenario():
            factory = lambda: {"CODEX_HOME": "/tmp/shadow-aim-test-auth", "OPENAI_API_KEY": "test-only",
                               "CODEX_API_KEY": "test-only", "OPENAI_BASE_URL": "https://invalid.example"}
            session = _Session(VoiceBridge(env_factory=factory), 0)
            create = AsyncMock(side_effect=RuntimeError("Stop before process launch"))
            try:
                with patch("companion.voice_bridge._load_media", return_value=(object(), object())), \
                     patch("companion.voice_bridge.asyncio.create_subprocess_exec", create), \
                     patch("companion.voice_bridge._command", return_value=["codex", "app-server"]) as command:
                    with self.assertRaisesRegex(RuntimeError, "Stop before"):
                        await session.connect(None)
                    environment = create.call_args.kwargs["env"]
                    self.assertEqual(environment["CODEX_HOME"], "/tmp/shadow-aim-test-auth")
                    self.assertNotIn("OPENAI_API_KEY", environment)
                    self.assertNotIn("CODEX_API_KEY", environment)
                    self.assertNotIn("OPENAI_BASE_URL", environment)
                    self.assertIsNone(command.call_args.args[0], "Codex must resolve from the selected app environment")
                    self.assertEqual(command.call_args.kwargs["environment"], environment)
            finally:
                if session.temp:
                    session.temp.cleanup()
        asyncio.run(scenario())

    def test_windows_native_and_npm_launch_prefixes_remain_argv_without_a_shell(self):
        async def scenario(prefix):
            environment = {"PATH": r"C:\Users\Dylan\Shadow Aim\runtime\bin;C:\Program Files\nodejs",
                           "CODEX_HOME": r"C:\Users\Dylan\Shadow Aim\Compte privé"}
            session = _Session(VoiceBridge(env_factory=lambda: dict(environment)), 0)
            create = AsyncMock(side_effect=RuntimeError("Stop before process launch"))
            try:
                with patch("companion.voice_bridge._load_media", return_value=(object(), object())), \
                     patch("companion.voice_bridge.sys.platform", "win32"), \
                     patch("companion.voice_bridge.asyncio.create_subprocess_exec", create), \
                     patch("companion.voice_bridge._command", return_value=prefix + ["app-server", "--stdio"]) as command:
                    with self.assertRaisesRegex(RuntimeError, "Stop before"):
                        await session.connect(None)
                    self.assertEqual(list(create.call_args.args[:len(prefix)]), prefix)
                    self.assertEqual(create.call_args.kwargs["env"], environment)
                    self.assertEqual(command.call_args.kwargs["environment"], environment)
                    self.assertIsNone(command.call_args.args[0])
                    self.assertEqual(create.call_args.kwargs["creationflags"], 0x08000000)
                    self.assertNotIn("shell", create.call_args.kwargs)
            finally:
                if session.temp:
                    session.temp.cleanup()

        prefixes = [
            [r"C:\Users\Dylan\Shadow Aim\runtime\bin\codex.exe"],
            [r"C:\Program Files\nodejs\node.exe", r"C:\Users\Dylan\Données\npm\node_modules\@openai\codex\bin\codex.js"],
        ]
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                asyncio.run(scenario(prefix))

    def test_pipe_writes_preserve_non_ascii_music_queries_as_utf8(self):
        async def scenario():
            data = []
            session = _Session(VoiceBridge(), 0)
            session.process = SimpleNamespace(returncode=None,
                stdin=SimpleNamespace(write=data.append, drain=AsyncMock()))
            message = {"id": "example", "result": {"title": "Björk — Jóga"}}
            await session.send(message)
            self.assertEqual(json.loads(data[0].decode("utf-8")), message)
            self.assertTrue(data[0].endswith(b"\n"))
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
