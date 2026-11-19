# Demo video pipeline

Source for the narrated walkthrough attached to the [`demo-v0.1`
release](https://github.com/Kaustubh-Rathi/adforge/releases/tag/demo-v0.1).

Everything here is reproducible: the dashboard is recorded live with Playwright,
the narration is synthesized with edge-tts, and ffmpeg assembles the cut. The
rendered files under `out/` are gitignored; only the sources are tracked.

## What each file does

| File | Job |
|---|---|
| `cards.html` + `cards.js` | Render the six full-frame cards (title, setup, setup-run, guardrails, honest, close) at 1600×900 |
| `capture.js` | Playwright: real recordings of create / run / approve / results, plus the still frames |
| `narration.json` | The 12-scene script: id, kind, lead-in, badge, narration text |
| `tts.py` | edge-tts narration → `out/audio/*.mp3` + per-scene `out/subs/*.srt` + `out/audio_manifest.json` |
| `build.py` | Cut segments, burn ASS captions, overlay the badge, mux voice, concat, fade, write the sidecar SRT |
| `reset_demo_tenant.py` | Delete the demo tenant's campaigns and checkpoints so a re-record starts empty |

## Prerequisites

- Node 20+ and Playwright with Chromium. `capture.js` uses `AF_CHROME` for the
  browser binary and defaults to a local Playwright install.
- ffmpeg with libass, libx264 and drawtext, on `PATH` or set `AF_FFMPEG` /
  pass `--ffmpeg`. The badge font defaults to Segoe UI on Windows and DejaVu
  Sans on Linux; set `AF_FONT` if neither is installed.
- `edge-tts` installed in the same virtualenv as the app (`pip install
  edge-tts`), and the API (`:8000`) and dashboard (`:8501`) running.
- The dashboard uses tenant `demo_tenant`; the approve scene expects
  `reviewer@adforge.local` to be allowed by `APP_REVIEWER_ALLOWLIST` or the
  allowlist to be empty in dev.

## Record

The scenes are separate recordings so the edit can cut between them:

```powershell
# Optional: start from zero campaigns in the demo tenant.
.\.venv\Scripts\python.exe scripts\demo_video\reset_demo_tenant.py

# Playwright resolution (if it is not installed next to this folder).
$env:NODE_PATH = "$env:TEMP\af_recdemo\node_modules"

node scripts\demo_video\capture.js stills    # out/stills/*
node scripts\demo_video\capture.js create    # out/capture/create.webm
node scripts\demo_video\capture.js run       # out/capture/run.webm   (also triggers the graph)
node scripts\demo_video\capture.js approve   # out/capture/approve.webm
node scripts\demo_video\capture.js results   # out/capture/results.webm
```

`run` uses the newest campaign and blocks until the graph parks at the review
gate; `approve` consumes the pending review. Run them in that order.

Playwright's video clock does not start with the script, so `build.py` cuts by
measured timestamps, not by log output. When re-recording, scan the new webm for
stable holds before editing the `PLAN` segments in `build.py`.

## Narrate and build

```powershell
# Cards (only needed when cards.html changes).
node scripts\demo_video\cards.js scripts\demo_video\out\cards

# All 12 voice tracks (or --only 07-run,09-results).
.\.venv\Scripts\python.exe scripts\demo_video\tts.py

# Full film, or a partial build of named scenes.
.\.venv\Scripts\python.exe scripts\demo_video\build.py
.\.venv\Scripts\python.exe scripts\demo_video\build.py --only 09-results
```

Output:

```
out/adforge-demo.mp4        the film (H.264 + AAC, 1600x900, 30 fps)
out/adforge-demo.srt        one sidecar subtitle file for the whole film
out/scenes/<id>.mp4         individual scenes, captions burned in
out/build/                  intermediate segments, ASS files, concat lists
```

`--keep-work` preserves `out/build/` between builds.

## Swapping in a recorded voice

Each scene is independent, so a human voice can replace the synthesized track
without touching the edit:

1. Record `out/audio/01-title.mp3` … `12-close.mp3` (same ids).
2. Write matching `out/subs/<id>.srt` cues, or keep the generated ones and
   adjust the timings.
3. Update `out/audio_manifest.json` with the new `audio_seconds` /
   `narration_seconds`, or re-run `tts.py` for the scenes you did not record.
4. Re-run `build.py`; captions are burned from the SRT and the scene lengths
   follow the manifest.

Use digits in narration text (`0.68`, `3600`, `KPI`): edge-tts intermittently
returns no audio for long spelled-out numerals. `tts.py` retries three times and
then fails loudly with that hint.
