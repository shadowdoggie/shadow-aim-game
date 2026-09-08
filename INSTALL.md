# Install Shadow Aim

Get the latest desktop bundle from [GitHub Releases](https://github.com/shadowdoggie/shadow-aim-game/releases).

## Windows 10/11, 64-bit

1. Extract the entire `shadow-aim-windows-x86_64.zip` into a permanent folder. Do not run it inside the ZIP viewer.
2. Double-click **Start Shadow Aim.cmd** to play. Godot and Python are included; no developer software needs to be installed.
3. For voice, music, and AI coaching, run **Setup Shadow Aim.cmd** once, then sign in with ChatGPT using the account controls in the game. Setup downloads the pinned audio packages directly from PyPI, verifies their checksums, installs official Codex for this app if missing, and adds a Start-menu shortcut.

Setup needs internet access. Windows may identify the download as an unsigned application; this project does not currently provide a signed installer. Keep the entire extracted folder together. Ordinary practice works without an account or microphone.

## Linux, 64-bit Intel/AMD

Extract `shadow-aim-linux-x86_64.tar.gz`, then run **Start Shadow Aim.sh**. The standard Godot runtime is included; no Godot editor or .NET installation is needed. Linux requires Python 3.11+ and a working desktop graphics/audio stack.

For voice, music, account setup, and an applications-menu shortcut, run **Setup Shadow Aim.sh** once. Setup installs the pinned audio dependencies into an app-owned Python environment and installs official Codex only if it is missing. It does not require administrator privileges or modify another Codex login.

If setup reports missing Python venv support, install your distribution's `python3-venv` package and retry. On systems whose file manager does not execute shell files, open a terminal in the extracted folder:

```sh
sh "Setup Shadow Aim.sh"
sh "Start Shadow Aim.sh"
```

## Account and voice availability

Use your ChatGPT subscription through Codex. AI access depends on your plan, model availability, account limits, and the installed Codex version. No API key or separately billed API fallback is used.

Juniper uses GPT-Live through Codex's experimental full-duplex realtime interface. Installing the app does not guarantee that this interface, the required model, or Juniper is available for every account. The game reports unavailable access. Voice has a [separate subscription allowance](https://learn.chatgpt.com/docs/pricing#chatgpt-voice-in-desktop). See [ChatGPT Voice](https://learn.chatgpt.com/docs/features/voice) for current product availability.

The microphone starts off. Connect voice explicitly and use headphones to avoid speaker feedback. Choose a music folder yourself; the app never automatically scans your computer for music.

## Troubleshooting and updates

From the extracted folder, run `python3 tools/setup.py --check` on Linux, or `runtime\python\python.exe tools\setup.py --check` on Windows. This checks local dependencies without signing in, recording audio, or scanning music folders.

On Linux, diagnostic logs are in `~/.local/state/aimcoach`. On Windows, they are in `%LOCALAPPDATA%\Shadow Aim\logs`. If a graphics driver fails, try adding `--rendering-method gl_compatibility` to the launch command.

To update, close the game and extract the new release into a new folder. Run setup again to point the shortcut at the new location. Saved rounds, settings, and account state live outside the release folder and remain intact. To uninstall the program, remove its extracted folder and menu shortcut. Saved data is retained; its locations are described in [PRIVACY.md](PRIVACY.md).

## Build a release from source

With Python 3.11+, Godot 4.7, and `uv` or `pip` installed:

```sh
python3 tools/package.py --release --target linux-x86_64
python3 tools/package.py --release --target windows-x86_64
```

The build verifies downloaded Godot/Python runtime checksums, exports the game pack, checks it without the source tree, and writes the archives under `build/`. Windows native execution is checked by the Windows CI job. A build made on Linux alone is not evidence that Windows execution passed.

Godot 4.7 can hit an editor-shutdown assertion after a completed export. The builder recognizes only that specific abort with a completed, nonempty pack and no other errors; the separate runtime smoke check must still pass. All other export errors fail the build.

## Included software

Godot's full copyright and license notices are in the release's `licenses/` folder. The Windows Python runtime includes its Python license. Optional audio dependencies are downloaded directly from PyPI during setup; their supplied licenses are installed with them. PyAV includes FFmpeg shared libraries; the corresponding projects and their source are available from [PyAV](https://github.com/PyAV-Org/PyAV) and [FFmpeg](https://ffmpeg.org/). Audio codec DLLs are not redistributed inside this release archive. Codex is installed separately from [OpenAI's official installer](https://learn.chatgpt.com/docs/codex/cli).
