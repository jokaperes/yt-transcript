# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

A YouTube transcription tool with two front-ends over one engine:

- **`transcribe_youtube.py`** — the engine. CLI + Tkinter GUI + a headless
  `--json-events` mode. Uses `yt-dlp` to fetch audio and `faster-whisper`
  (CTranslate2, NVIDIA GPU) to transcribe.
- **`electron/`** — a desktop app (Electron) that drives the engine as a bundled
  sidecar exe and auto-updates itself from GitHub Releases.

The engine is the source of truth. The Electron app is a front-end only — it
never reimplements transcription logic, it spawns the engine and parses its
output.

## Layout

```
transcribe_youtube.py          # engine: CLI / Tkinter GUI / --json-events
requirements-windows.txt       # engine deps (faster-whisper, yt-dlp, ...)
build-engine.ps1               # PyInstaller build -> electron/engine/*.exe (+ yt-dlp.exe)
.github/workflows/windows-release.yml  # CI: build engine + installer, publish release
electron/
  package.json                 # electron-builder config + publish target
  main.js                      # main process: spawn engine, IPC, auto-update, tray
  preload.js                   # contextBridge window.api (contextIsolation on)
  renderer/                    # UI (index.html, style.css, renderer.js)
  build/icon.{png,ico}         # app icons (tracked)
  engine/                      # bundled exes — built in CI, gitignored
```

## The engine <-> Electron contract

Electron runs the engine with `--json-events`. The engine emits one JSON object
per line on stdout; cancellation is a `cancel\n` line written to engine stdin.

Event types (see `run_json_events_batch()` / `emit_event()` in
`transcribe_youtube.py`, and `handleEngineEvent()` in `renderer/renderer.js`):

- `batch_start { total }`
- `queue { current, total, url }`
- `progress { phase, percent, detail }` — phase ∈ download/extract/model/transcribe/write
- `video_done { title, files }`
- `video_failed { url, error }`
- `batch_done { completed, failed, total, files, cancelled }`

**If you change any event name or field, update both sides** — the Python emitter
and the JS consumer — or the UI silently breaks. The renderer's `PHASE_WEIGHTS`
map also depends on the `phase` values.

## Build & release flow

There is **no local Windows/Electron build here** — this repo is developed on an
ARM Linux box that cannot run Electron or compile the Windows engine. All real
builds happen in CI on `windows-latest`. Validate changes statically:

- `python3 -m py_compile transcribe_youtube.py`
- `node --check electron/main.js electron/preload.js electron/renderer/renderer.js`
- JSON-validate `electron/package.json`

Releases are cut by pushing a tag:

1. Bump `version` in `electron/package.json`.
2. `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. CI runs `build-engine.ps1` (PyInstaller -> `electron/engine/yt-transcript.exe`
   + downloads `yt-dlp.exe`), then `npm install` + `npm run publish`
   (electron-builder) to produce `YT-Transcript-Setup-X.Y.Z.exe` + `latest.yml`
   and publish them to GitHub Releases. The installed app self-updates from that
   feed.

Keep the git tag, `electron/package.json` `version`, and the installer filename
in sync.

## Conventions & guardrails

- Match the surrounding code style; the engine is plain stdlib-flavored Python,
  the Electron side is vanilla JS (no framework).
- Never commit auth material: `cookies.txt`, `*.cookies.txt`,
  `normalized-youtube-cookies.txt` are gitignored — keep it that way.
- `electron/engine/` exes and `electron/node_modules/` are gitignored (built in
  CI); the `electron/build/` icons are intentionally tracked.
- `ffmpeg` is an external runtime dependency on every platform — it is not
  bundled. Don't assume it's importable/portable.
- Default transcription language is Portuguese (`pt`); don't change defaults
  without a reason.
