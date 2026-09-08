# Shadow Aim data and privacy

Shadow Aim stores training history on your computer. It has no developer-hosted account service, advertising, or analytics endpoint. AI features connect to OpenAI through your Codex/ChatGPT sign-in.

## What stays local

- Full aim recordings and replays, settings, sensitivity experiments, saved coaching, and training history.
- Your selected music folder, its song catalog, and cached audio conversions. Music files are read only from the folder you choose; directory symlinks are not followed. The app does not upload music files.
- App-specific account state and diagnostic logs. Existing installations may initially use an existing Codex sign-in. Explicit account login/logout actions use the app's private account scope.

## What AI features send

Coaching sends measured results, selected trace evidence, relevant earlier rounds and advice, and questions you ask. When voice is connected, your microphone audio and current training context are sent to GPT-Live. Game context includes settings, current drill, provisional performance measurements, and visible controls. Song searches and playback requests may send song titles, artists, albums, and opaque track identifiers so Juniper can identify your selection. Raw local music paths are not needed by the voice model.

OpenAI handles this data according to your account's applicable policies and settings. See [OpenAI's privacy policy](https://openai.com/policies/privacy-policy/) and [ChatGPT data controls](https://help.openai.com/en/articles/7730893-data-controls-faq). This app does not promise a separate retention policy for OpenAI's services.

## Controls and storage

Voice starts disconnected. Connecting voice enables microphone capture; stopping voice stops capture. Ordinary training remains available when signed out or offline. Account sign-out and deleting the program do not erase your training history.

On Linux, companion data is under `~/.local/share/aimcoach` and logs under `~/.local/state/aimcoach`. On Windows, companion data is under `%LOCALAPPDATA%\Shadow Aim` and logs in its `logs` folder. `AIMCOACH_DATA_DIR` and `AIMCOACH_STATE_DIR` can override these locations. Godot stores game preferences in its application user-data directory (`~/.local/share/godot/app_userdata/Shadow Aim` on typical Linux installations, `%APPDATA%\Godot\app_userdata\Shadow Aim` on Windows).

Close the game before copying, backing up, or deleting local data. Deleting the companion data directory removes saved rounds, replay traces, coaching history, music catalog/cache, and app-specific account state. It does not delete your original music files or another Codex installation's credentials. Diagnostic logs can contain technical identifiers and context; review them before sharing publicly.
