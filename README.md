# Shadow Aim

A native aim trainer for Linux and Windows. Train with Juniper, review recorded measurements, compare sensitivities, and play your own music. Coaching is spoken; screens show controls, scores, and replays.

## Download and play

[Visit the website](https://shadowaim.shadowdog.cat/) for the latest download for your operating system.

Download the archive for your system from [Releases](https://github.com/shadowdoggie/shadow-aim-game/releases). Follow the included [installation instructions](INSTALL.md). To run a source checkout, use `python3 launch.py` with Godot 4.7 and Python 3.11 or newer.

Open **ChatGPT account** to sign in. Coaching uses your ChatGPT subscription through Codex. Your plan, remaining usage, and access to the requested models determine availability. There is no API-key billing fallback. Practice remains available without AI access.

Start a guided baseline or choose **Precision clicking**, **Smooth tracking**, **Target switching**, or **Reactive tracking**. Reactive tracking adds unpredictable, reproducible direction changes and has its own benchmark history. Settings include sensitivity, DPI, field of view, target size, tracking speed, duration, and frame limit.

- **Left click:** shoot; hold while tracking.
- **Escape:** pause. Resume preserves the round and excludes paused time.
- **V during play:** connect or stop Juniper.
- **Hear coaching:** listen to a saved review without requesting another analysis.

## Coaching and voice

Reviews use **Sol (`gpt-5.6-sol`) at high effort**. A targeted comparison favored Sol over Luna for interpreting the evidence correctly; see [the evaluation](docs/model-evaluation.md). Reviews use matching baselines, previous advice, and measured progress. They run between rounds, after a completed guided baseline, or when requested—not every frame. Practice blocks reuse their prescription before a retest.

Voice uses **GPT-Live (`gpt-live-1-codex`), full-duplex v3, and Juniper**. Microphone capture and speech playback run simultaneously. There is no older voice-mode or text-to-speech fallback. While connected, Juniper receives provisional aim measurements roughly every five seconds and saved round context. These updates do not each request a Sol analysis. Conversation and delegated tasks can consume additional subscription usage.

Voice starts off. **Talk with Juniper** enables the microphone; stopping voice ends capture and disconnects. Use headphones: this native integration does not provide acoustic echo cancellation. The voice, music, and game sliders are independently saved; Juniper can change them when asked.

This integration uses Codex app-server's experimental realtime interface. It checks the requested voice, protocol, model, and effort rather than silently substituting another. Availability and [voice usage limits](https://learn.chatgpt.com/docs/pricing#chatgpt-voice-in-desktop) can differ from ordinary coaching, and Codex updates can affect compatibility.

## Music and sensitivity

In **Music**, choose a folder. Shadow Aim indexes supported audio in its subfolders without following directory symlinks. Search and play locally, or ask Juniper for a song or artist. Ambiguous requests require a selection. Juniper reports success only after the game confirms the action. Music keeps playing across menus and rounds.

**Find my sensitivity** compares your original setting with values 20% lower and higher, usually in 12 short rounds. Each has five unscored adaptation seconds and 20 measured seconds. Matched target seeds, reversed repeat order, accuracy, speed, and tracking measurements inform the comparison. Later comparable practice can revise an earlier result. No test silently changes your saved sensitivity; applying a suggestion is explicit. Saved comparisons reopen without another analysis request.

## Accounts and data

New sign-ins use a private Shadow Aim account scope. Existing installations can continue using their Codex sign-in until choosing an account action; signing out of Shadow Aim does not sign out a separate Codex installation. Account switching stops active coaching and voice first.

Aim recordings, history, settings, and the music catalog stay on your computer. Coaching sends selected measurements, trace evidence, relevant earlier results, and conversation context to OpenAI. Voice sends microphone audio while enabled. Music audio is played locally; song metadata can be included when handling music requests. No recordings, credentials, library, or personal history are bundled in downloads.

The coach cannot infer grip, posture, or physical causes from mouse traces. Immediate improvements do not prove lasting improvement or transfer to another game. You can review the recorded evidence through native replays.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest companion.voice_bridge.test_bridge -v
python3 launch.py --headless --audio-driver Dummy --script tests/ui_workflows_smoke.gd
python3 launch.py --headless --audio-driver Dummy --script tests/voice_audio_smoke.gd
python3 tools/package.py
```

CI checks Linux and Windows without real account changes, microphone recording, or model requests. See [installation and packaging](INSTALL.md) for distribution details.
