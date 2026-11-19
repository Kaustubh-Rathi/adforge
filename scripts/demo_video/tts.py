"""Synthesize the narration and per-scene captions with edge-tts.

For every scene in ``narration.json`` this writes:

    out/audio/<id>.mp3   the voice track
    out/subs/<id>.srt    captions, already shifted by the scene's lead-in

edge-tts emits sentence-level cues. Long sentences are split at word
boundaries and the cue duration is distributed by character count, which is
close enough to the speaking rate that captions stay in sync. Swapping in a
recorded voice only requires replacing the mp3s and running build.py.

Usage:
    python scripts/demo_video/tts.py [--voice en-US-AndrewNeural]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
AUDIO = OUT / "audio"
SUBS = OUT / "subs"
NARRATION = HERE / "narration.json"

VOICE = "en-US-AndrewNeural"
RATE = "-4%"
MAX_CHARS = 46
DEFAULT_FFMPEG = os.environ.get("AF_FFMPEG") or shutil.which("ffmpeg")
MIN_CUE = 1.1

_CUE_RE = re.compile(
    r"(\d+)\s*\n(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})\s*\n(.*?)(?:\n\n|$)",
    re.S,
)


def seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def stamp(value: float) -> str:
    value = max(0.0, value)
    hours, rest = divmod(value, 3600)
    minutes, rest = divmod(rest, 60)
    secs, millis = divmod(rest, 1)
    return f"{int(hours):02d}:{int(minutes):02d}:{int(secs):02d},{round(millis * 1000):03d}"


def split_text(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Split a sentence into balanced caption chunks at word boundaries."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        count = -(-len(rest) // limit)  # ceil
        target = len(rest) / count
        low, high = max(1, int(target) - 12), min(len(rest) - 1, int(target) + 12)
        spaces = [i for i in range(low, high + 1) if rest[i] == " "]
        punctuation = [i for i in spaces if rest[i - 1] in ",;:"]
        if punctuation:
            cut = punctuation[0]
        elif spaces:
            cut = min(spaces, key=lambda i: abs(i - target))
        else:
            cut = int(target)
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        chunks.append(rest)
    return chunks


def parse_srt(raw: str) -> list[tuple[float, float, str]]:
    cues: list[tuple[float, float, str]] = []
    for match in _CUE_RE.finditer(raw.strip() + "\n\n"):
        start = seconds(*match.group(2, 3, 4, 5))
        end = seconds(*match.group(6, 7, 8, 9))
        text = " ".join(match.group(10).split())
        cues.append((start, end, text))
    return cues


def rewrite_cues(
    cues: list[tuple[float, float, str]], lead: float
) -> list[tuple[float, float, str]]:
    """Split long cues and shift everything by the scene's lead-in."""
    out: list[tuple[float, float, str]] = []
    for start, end, text in cues:
        text = display(text)
        chunks = split_text(text)
        span = end - start
        shares = [(len(chunk) + 1) for chunk in chunks]
        total = sum(shares)
        durations = [max(MIN_CUE, span * share / total) for share in shares]
        scale = span / sum(durations)
        durations = [d * scale for d in durations]

        cursor = start
        for index, (chunk, length) in enumerate(zip(chunks, durations, strict=True)):
            cue_end = end if index == len(chunks) - 1 else cursor + length
            out.append((cursor + lead, cue_end + lead, chunk))
            cursor = cue_end
    return out


#: TTS needs letters spelled out; captions should read normally.
_DISPLAY = (
    ("0.68 times on spend", "0.68× on spend"),
    ("at 0.38", "at 0.38×"),
    ("returns 1.22", "returns 1.22×"),
    ("about 3600 dollars", "about $3,600"),
    ("zero point six eight", "0.68×"),
    ("zero point three eight", "0.38×"),
    ("one point two two", "1.22×"),
    ("zero point nine six", "0.96×"),
    ("zero point seven six", "0.76×"),
    ("zero point nine times", "0.9×"),
    ("zero point nine", "0.9×"),
    ("three thousand six hundred", "$3,600"),
    ("fifteen-thousand-dollar", "$15,000"),
    ("twelve hundred", "1,200"),
    ("eight times", "8×"),
    ("eight thousand", "8,000"),
    ("eight five zero one", "8501"),
    ("thirty-day", "30-day"),
    ("K P I", "KPI"),
    ("R O I", "ROI"),
    ("A P I", "API"),
    ("B two B", "B2B"),
)


def display(text: str) -> str:
    for spoken, written in _DISPLAY:
        text = text.replace(spoken, written)
    return text


def write_srt(cues: list[tuple[float, float, str]], path: Path) -> None:
    lines: list[str] = []
    for index, (start, end, text) in enumerate(cues, 1):
        lines.append(f"{index}\n{stamp(start)} --> {stamp(end)}\n{text}\n")
    path.write_text("\n".join(lines), encoding="utf-8")


def audio_duration(path: Path, ffmpeg: Path) -> float:
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", result.stderr)
    if not match:
        raise RuntimeError(f"could not read duration of {path}")
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--voice", default=VOICE)
    parser.add_argument("--rate", default=RATE)
    parser.add_argument("--only", default="", help="comma-separated scene ids")
    parser.add_argument(
        "--ffmpeg",
        default=DEFAULT_FFMPEG,
        help="ffmpeg binary (default: AF_FFMPEG or PATH)",
    )
    args = parser.parse_args()

    if not args.ffmpeg:
        raise SystemExit("ffmpeg not found; install it, set AF_FFMPEG, or pass --ffmpeg")
    ffmpeg = Path(args.ffmpeg)
    if not ffmpeg.exists():
        raise SystemExit(f"ffmpeg not found: {ffmpeg}")
    edge = shutil.which("edge-tts")
    if edge is None:
        sibling = Path(sys.executable).with_name("edge-tts.exe" if os.name == "nt" else "edge-tts")
        edge = str(sibling) if sibling.exists() else None
    if edge is None:
        raise SystemExit("edge-tts not found; pip install edge-tts in this environment")

    AUDIO.mkdir(parents=True, exist_ok=True)
    SUBS.mkdir(parents=True, exist_ok=True)

    scenes = json.loads(NARRATION.read_text(encoding="utf-8"))
    wanted = {value.strip() for value in args.only.split(",") if value.strip()}
    selected = [scene for scene in scenes if not wanted or scene["id"] in wanted]
    missing = wanted - {scene["id"] for scene in selected}
    if missing:
        raise SystemExit(f"unknown scene id(s): {', '.join(sorted(missing))}")

    manifest: list[dict[str, object]] = []

    for scene in selected:
        scene_id = scene["id"]
        text = scene["narration"]
        lead = float(scene.get("lead", 0.45))
        mp3 = AUDIO / f"{scene_id}.mp3"
        raw_srt = AUDIO / f"{scene_id}.raw.srt"

        print(f"[{scene_id}] {text[:60]}...")
        for attempt in range(3):
            result = subprocess.run(
                [
                    str(edge),
                    "-t",
                    text,
                    "-v",
                    args.voice,
                    f"--rate={args.rate}",
                    "--write-media",
                    str(mp3),
                    "--write-subtitles",
                    str(raw_srt),
                    "--no-progress",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                break
            reason = result.stderr.strip().splitlines()[-1:] or ["unknown error"]
            print(f"  attempt {attempt + 1} failed: {reason[0]}")
            time.sleep(2)
        else:
            raise SystemExit(
                f"edge-tts failed for {scene_id} after 3 attempts; "
                "spell numerals as digits rather than words"
            )

        cues = rewrite_cues(parse_srt(raw_srt.read_text(encoding="utf-8")), lead)
        write_srt(cues, SUBS / f"{scene_id}.srt")
        duration = audio_duration(mp3, ffmpeg)
        manifest.append(
            {
                "id": scene_id,
                "audio": str(mp3.relative_to(HERE)).replace("\\", "/"),
                "srt": str((SUBS / f"{scene_id}.srt").relative_to(HERE)).replace("\\", "/"),
                "audio_seconds": round(duration, 3),
                "lead": lead,
                "narration_seconds": round(lead + duration, 3),
            }
        )

    manifest_path = OUT / "audio_manifest.json"
    existing: dict[str, dict[str, object]] = {}
    if manifest_path.exists():
        for item in json.loads(manifest_path.read_text(encoding="utf-8")):
            existing[str(item["id"])] = item
    for item in manifest:
        existing[str(item["id"])] = item

    ordered = [existing[scene["id"]] for scene in scenes]
    manifest_path.write_text(json.dumps(ordered, indent=2), encoding="utf-8")
    total = sum(float(item["narration_seconds"]) for item in ordered)
    print(f"\n{len(manifest)} scene(s) synthesised, {total / 60:.1f} minutes total")


if __name__ == "__main__":
    main()
