"""Assemble the AdForge demo video.

Inputs, all produced by the other files in this directory:

    out/cards/card-*.png     node cards.js out/cards
    out/stills/*.png         node capture.js stills
    out/capture/*.webm       node capture.js create|run|approve|results
    out/audio/*.mp3          python tts.py
    out/subs/*.srt           python tts.py

Outputs:

    out/scenes/<id>.mp4      one normalised scene per narration entry
    out/adforge-demo.mp4     the finished video (1600x900, 30 fps, AAC)
    out/adforge-demo.srt     the same captions as a sidecar file

Usage:
    python scripts/demo_video/build.py
    python scripts/demo_video/build.py --only 07-run,08-gate
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
CARDS = OUT / "cards"
STILLS = OUT / "stills"
CAPTURE = OUT / "capture"
AUDIO = OUT / "audio"
SUBS = OUT / "subs"
SCENES = OUT / "scenes"
BUILD = OUT / "build"

WIDTH, HEIGHT, FPS = 1600, 900, 30

DEFAULT_FFMPEG = shutil.which("ffmpeg") or (
    r"C:\Users\Kaustuvh\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    r"\ffmpeg-9.0.1-full_build\bin\ffmpeg.exe"
)

#: libass style. PlayRes matches the frame, so sizes are real pixels.
ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1600
PlayResY: 900
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Segoe UI Semibold,34,&H00FFFFFF,&H000000FF,&H00000000,&H99000000,0,0,0,0,100,100,0,0,3,10,0,2,90,90,36,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ass_stamp(value: float) -> str:
    total = round(max(0.0, value) * 100)
    hours, rest = divmod(total, 360_000)
    minutes, rest = divmod(rest, 6_000)
    secs, centis = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def srt_to_ass(srt: Path, ass: Path) -> None:
    """Convert the scene's SRT into ASS with a known, video-sized canvas."""
    import re

    lines: list[str] = [ASS_HEADER]
    raw = srt.read_text(encoding="utf-8")
    for start, end, text in re.findall(
        r"(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\n(.*?)(?=\n\n|\Z)",
        raw,
        re.S,
    ):
        body = text.strip().replace("\n", r"\N").replace("{", r"\{").replace("}", r"\}")
        lines.append(
            "Dialogue: 0,"
            f"{ass_stamp(_seconds(start))},{ass_stamp(_seconds(end))},"
            f"Default,,0,0,0,,{body}"
        )
    ass.write_text("\n".join(lines) + "\n", encoding="utf-8")


@dataclass
class Segment:
    """A slice of a recording, played at a constant speed."""

    start: float
    end: float
    speed: float = 1.0

    @property
    def duration(self) -> float:
        return (self.end - self.start) / self.speed


@dataclass
class Scene:
    id: str
    kind: str  # card | still | clip
    source: str
    tail: float = 1.2
    badge: str | None = None
    segments: list[Segment] = field(default_factory=list)

    def path(self) -> Path:
        return HERE / self.source


PLAN: list[Scene] = [
    Scene("01-title", "card", "out/cards/card-title.png", tail=1.6),
    Scene("02-overview", "still", "out/stills/home.png", tail=1.2),
    Scene("03-setup", "card", "out/cards/card-setup.png", tail=1.4),
    Scene("04-setup-run", "card", "out/cards/card-setup-run.png", tail=1.4),
    Scene("05-tour", "still", "out/stills/campaigns.png", tail=1.2),
    Scene(
        "06-create",
        "clip",
        "out/capture/create.webm",
        tail=1.2,
        # Fill the brief, then cut to the moment the form locks and the
        # campaign card appears.
        segments=[Segment(3.5, 19.5, 1.5), Segment(27.0, 31.5, 1.0)],
    ),
    Scene(
        "07-run",
        "clip",
        "out/capture/run.webm",
        tail=0.8,
        badge="live run - time-lapse",
        # Empty ledger, then only the clean holds: 1, 2, 3 and 4 finished runs.
        # The repaint gaps between rows are cut out rather than shown as white.
        segments=[
            Segment(5, 12, 6.0),
            Segment(93.5, 100, 1.5),
            Segment(106, 115, 1.5),
            Segment(121, 135, 1.5),
            Segment(142, 148, 1.5),
            Segment(155, 158.5, 1.5),
        ],
    ),
    Scene(
        "08-gate",
        "clip",
        "out/capture/approve.webm",
        tail=0.8,
        # Queue header, packet, the filled reviewer email, then the real ~57 s
        # spinner and the cleared queue with the approval toast.
        segments=[
            Segment(5.5, 10, 1.0),
            Segment(10, 16, 0.7),
            Segment(21.5, 23.5, 0.8),
            Segment(31.5, 88, 11.0),
            Segment(91.8, 94.0, 0.6),
        ],
    ),
    Scene(
        "09-results",
        "clip",
        "out/capture/results.webm",
        tail=1.0,
        # Seven-run ledger, the verdict card, then the per-channel ROAS chart.
        segments=[
            Segment(4.6, 8.6, 1.4),
            Segment(24, 34.5, 0.9),
            Segment(35, 53.8, 1.2),
        ],
    ),
    Scene("10-guardrails", "card", "out/cards/card-guardrails.png", tail=1.4),
    Scene("11-honest", "card", "out/cards/card-honest.png", tail=1.5),
    Scene("12-close", "card", "out/cards/card-close.png", tail=1.8),
]


def run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, errors="replace")
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-30:])
        raise SystemExit(f"ffmpeg failed:\n{' '.join(cmd)}\n\n{tail}")


def probe_duration(path: Path, ffmpeg: Path) -> float:
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        errors="replace",
    )
    for line in result.stderr.splitlines():
        if "Duration:" in line:
            stamp = line.split("Duration:")[1].split(",")[0].strip()
            h, m, s = stamp.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    raise SystemExit(f"could not read duration: {path}")


def image_scene(scene: Scene, duration: float, ffmpeg: Path, out: Path) -> None:
    """A still with a slow, centred zoom, encoded to the project format."""
    source = scene.path()
    zoom = "min(1+0.00022*on,1.035)"
    vf = (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=0xF7F8FA,"
        f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d=1:s={WIDTH}x{HEIGHT}:fps={FPS},setsar=1"
    )
    run(
        [
            str(ffmpeg),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-framerate",
            str(FPS),
            "-t",
            f"{duration:.3f}",
            "-i",
            str(source),
            "-vf",
            vf,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ]
    )


def clip_segment(scene: Scene, segment: Segment, index: int, ffmpeg: Path, out: Path) -> None:
    vf = (
        f"setpts=(PTS-STARTPTS)/{segment.speed},"
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=0xF7F8FA,"
        f"fps={FPS},setsar=1"
    )
    run(
        [
            str(ffmpeg),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{segment.start:.3f}",
            "-t",
            f"{segment.end - segment.start:.3f}",
            "-i",
            str(scene.path()),
            "-vf",
            vf,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ]
    )


def clip_scene(scene: Scene, duration: float, ffmpeg: Path, out: Path, work: Path) -> None:
    """Normalise each segment, concat them, then pad or trim to the narration."""
    pieces: list[Path] = []
    for index, segment in enumerate(scene.segments, 1):
        piece = work / f"{scene.id}-seg{index}.mp4"
        clip_segment(scene, segment, index, ffmpeg, piece)
        pieces.append(piece)

    listing = work / f"{scene.id}-segments.txt"
    listing.write_text(
        "".join(
            f"file '{(work / f'{scene.id}-seg{i}.mp4').as_posix()}'\n"
            for i in range(1, len(pieces) + 1)
        ),
        encoding="utf-8",
    )
    joined = work / f"{scene.id}-joined.mp4"
    run(
        [
            str(ffmpeg),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-c",
            "copy",
            str(joined),
        ]
    )

    # Allow a short freeze at the end when the recording is shorter than the
    # narration; the final mux trims anything longer.
    vf = "tpad=stop_mode=clone:stop_duration=8"
    run(
        [
            str(ffmpeg),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(joined),
            "-vf",
            vf,
            "-t",
            f"{duration:.3f}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ]
    )


def mux(scene: Scene, duration: float, lead: float, ffmpeg: Path, silent: Path, out: Path) -> None:
    """Burn the captions, add the voice track and lock the duration."""
    audio = AUDIO / f"{scene.id}.mp3"
    ass = BUILD / f"{scene.id}.ass"
    srt_to_ass(SUBS / f"{scene.id}.srt", ass)
    ass_arg = ass.relative_to(HERE).as_posix()

    video = f"[0:v]subtitles=filename={ass_arg}"
    if scene.badge:
        text = scene.badge.replace(":", r"\:").replace("'", "")
        video += (
            f",drawtext=text='{text}':fontfile='C\\:/Windows/Fonts/segoeui.ttf':"
            f"fontsize=22:fontcolor=white:box=1:boxcolor=0x12181F@0.72:boxborderw=12:"
            f"x=w-tw-34:y=30"
        )
    video += "[v]"

    filters = f"{video};[1:a]adelay={int(lead * 1000)}:all=1,apad[a]"
    run(
        [
            str(ffmpeg),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(silent),
            "-i",
            str(audio),
            "-filter_complex",
            filters,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-t",
            f"{duration:.3f}",
            "-movflags",
            "+faststart",
            str(out),
        ]
    )


def write_sidecar(manifest: list[dict], durations: dict[str, float], path: Path) -> None:
    """One SRT for the whole film, with every scene shifted into place."""
    import re

    offset = 0.0
    blocks: list[str] = []
    number = 0
    for item in manifest:
        scene_id = str(item["id"])
        raw = (SUBS / f"{scene_id}.srt").read_text(encoding="utf-8")
        for start, end, text in re.findall(
            r"(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\n(.*?)(?=\n\n|\Z)",
            raw,
            re.S,
        ):
            number += 1
            blocks.append(
                f"{number}\n{shift(start, offset)} --> {shift(end, offset)}\n{text.strip()}\n"
            )
        offset += durations[scene_id]
    path.write_text("\n".join(blocks), encoding="utf-8")


def _seconds(stamp: str) -> float:
    h, m, rest = stamp.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _stamp(value: float) -> str:
    hours, rest = divmod(max(0.0, value), 3600)
    minutes, rest = divmod(rest, 60)
    secs, millis = divmod(rest, 1)
    return f"{int(hours):02d}:{int(minutes):02d}:{int(secs):02d},{round(millis * 1000):03d}"


def shift(stamp: str, offset: float) -> str:
    return _stamp(_seconds(stamp) + offset)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ffmpeg", default=DEFAULT_FFMPEG)
    parser.add_argument("--only", default="", help="comma-separated scene ids")
    parser.add_argument("--keep-work", action="store_true")
    args = parser.parse_args()

    ffmpeg = Path(args.ffmpeg)
    if not ffmpeg.exists():
        raise SystemExit(f"ffmpeg not found: {ffmpeg}")
    node = shutil.which("node")

    manifest = json.loads((OUT / "audio_manifest.json").read_text(encoding="utf-8"))
    timing = {str(item["id"]): item for item in manifest}

    SCENES.mkdir(parents=True, exist_ok=True)
    if BUILD.exists() and not args.keep_work:
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True, exist_ok=True)

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    plan = [scene for scene in PLAN if not only or scene.id in only]

    if any(scene.kind == "card" for scene in plan) and not CARDS.exists():
        if node:
            print("rendering cards...")
            subprocess.run(["node", "cards.js", "out/cards"], cwd=HERE, check=True)
        else:
            raise SystemExit("out/cards is missing; run: node cards.js out/cards")

    for scene in plan:
        if not scene.path().exists():
            raise SystemExit(f"missing source for {scene.id}: {scene.path()}")
        if scene.id not in timing:
            raise SystemExit(f"no narration timing for {scene.id}")

        item = timing[scene.id]
        duration = float(item["narration_seconds"]) + scene.tail
        lead = float(item["lead"])
        print(
            f"[{scene.id}] {scene.kind:5s} {duration:5.1f}s "
            f"(voice {float(item['audio_seconds']):.1f}s)"
        )

        silent = BUILD / f"{scene.id}-silent.mp4"
        if scene.kind == "clip":
            clip_scene(scene, duration, ffmpeg, silent, BUILD)
        else:
            image_scene(scene, duration, ffmpeg, silent)

        mux(scene, duration, lead, ffmpeg, silent, SCENES / f"{scene.id}.mp4")

    if only and len(only) != len(PLAN):
        print("\npartial build; skipping concat")
        return

    durations = {}
    for scene in PLAN:
        durations[scene.id] = probe_duration(SCENES / f"{scene.id}.mp4", ffmpeg)

    listing = BUILD / "scenes.txt"
    listing.write_text(
        "".join(f"file '{(SCENES / f'{scene.id}.mp4').as_posix()}'\n" for scene in PLAN),
        encoding="utf-8",
    )
    joined = BUILD / "joined.mp4"
    print("concatenating...")
    run(
        [
            str(ffmpeg),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-c",
            "copy",
            str(joined),
        ]
    )

    total = sum(durations.values())
    final = OUT / "adforge-demo.mp4"
    fade_out = max(0.0, total - 0.9)
    print(f"finishing ({total / 60:.2f} min)...")
    run(
        [
            str(ffmpeg),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(joined),
            "-vf",
            f"fade=t=in:st=0:d=0.6,fade=t=out:st={fade_out:.2f}:d=0.9",
            "-af",
            f"afade=t=in:st=0:d=0.6,afade=t=out:st={max(0.0, total - 1.4):.2f}:d=1.4",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(final),
        ]
    )

    write_sidecar(manifest, durations, OUT / "adforge-demo.srt")
    print(f"wrote {final} ({final.stat().st_size / 1_000_000:.1f} MB, {total / 60:.2f} min)")


if __name__ == "__main__":
    main()
