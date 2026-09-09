# Shadow Aim

A native aim trainer for Linux and Windows. Train with Juniper, review recorded measurements, compare sensitivities, and play your own music. Coaching is spoken; screens show controls, scores, and replays.

## Download and play

[Visit the website](https://shadowaim.shadowdog.cat/) for the latest download for your operating system.

Download the archive for your system from [Releases](https://github.com/shadowdoggie/shadow-aim-game/releases). Follow the included [installation instructions](INSTALL.md). To run a source checkout, use `python3 launch.py` with Godot 4.7 and Python 3.11 or newer.

Open **ChatGPT account** to sign in. Coaching uses your ChatGPT subscription through Codex. Your plan, remaining usage, and access to the requested models determine availability. There is no API-key billing fallback. Practice remains available without AI access.

The guided baseline measures **Precision clicking**, **Smooth tracking**, **Reactive tracking**, and **Target switching**. Afterward, the main **Practice** button follows saved coaching, resumes unfinished practice, and retests at the original difficulty. You can keep practising while a review runs: a ready approved plan is used when available, otherwise the same drill and settings continue. Completing another free round does not discard the review or restart an already finished prescription. This progress survives restarts. Reactive tracking has its own artwork and benchmark history. All four modes also remain available for free practice.

- **Left click:** shoot; hold while tracking.
- **Escape:** pause. Resume preserves the round and excludes paused time.
- **V during play:** connect or stop Juniper.
- **Hear coaching:** listen to a saved review without requesting another analysis.

## Coaching and voice

Reviews use **Sol (`gpt-5.6-sol`) at medium effort** with the same structured evidence checks. Compact requests preserve measured results, matching baselines and previous goals while omitting replay trace examples. Spoken tips use short, ordinary sentences without statistics. A targeted comparison favored Sol over Luna. A later low-effort trial still repeated the earlier slowing cue, so medium effort remains selected. Compact requests have not yet been timed at medium effort; these small checks do not establish consistently fast responses or general coaching quality. See [the evaluation](docs/model-evaluation.md). Reviews use matching baselines, previous advice, and measured progress. They run between rounds, after a completed guided baseline, or when requested—not every frame. Practice blocks reuse their prescription before a retest.

Voice uses **GPT-Live (`gpt-live-1-codex`), full-duplex v3, and Juniper**. Microphone capture and speech playback run simultaneously. There is no older voice-mode or text-to-speech fallback. While connected, Juniper receives provisional aim measurements roughly every five seconds and saved round context. These updates do not each request a Sol analysis. App controls use a separate Luna/low backing thread; one live check applied volume and mix requests in about 2.7 seconds after speech, with network latency still variable. Conversation and delegated tasks can consume additional subscription usage.

Juniper acknowledges a finished round while the detailed coach works. Spoken advice uses brief, everyday instructions and avoids numbers unless you ask for them. New clicking recordings include target-relative shot placement, center/edge percentages and directional bias for analysis. Older recordings remain explicitly unknown for these measurements.

Juniper can save short personal notes about interests, preferences, names and goals you share, so these can carry over between visits. Ask her to remember, correct or forget something; Settings also lets you view and clear saved notes. Personal memory is stored locally for each signed-in account, separately from round history. It is a limited set of notes, not a complete transcript or cross-device memory.

Voice starts off. **Talk with Juniper** enables the microphone; stopping voice ends capture and disconnects. Use headphones: this native integration does not provide acoustic echo cancellation. The voice, music, and game sliders are independently saved; Juniper can change them when asked.

This integration uses Codex app-server's experimental realtime interface. It checks the requested voice, protocol, model, and effort rather than silently substituting another. Availability and [voice usage limits](https://learn.chatgpt.com/docs/pricing#chatgpt-voice-in-desktop) can differ from ordinary coaching, and Codex updates can affect compatibility.

## Music and sensitivity

In **Music**, choose a folder once. Shadow Aim saves it immediately, checkpoints unfinished scans, and resumes them after restart. Juniper gets current scan progress and partial search results. Mood, genre, or artist requests automatically choose a queue using music tags and folder labels; these are metadata matches, not audio classification. Next and automatic playback stay within that queue. Specific songs continue through related album, artist, or folder tracks; ambiguous song requests may need clarification. Juniper reports the actual track only after the game confirms playback. Music keeps playing across menus and rounds.

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
