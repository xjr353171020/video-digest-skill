#!/usr/bin/env python3
"""
Video ingestion helper for the Codex video-digest skill.

Strategy:
1) fetch metadata
2) try creator/auto subtitles without downloading video
3) normalize subtitle text
4) if no subtitles: download best audio and use local Whisper CLI
5) optionally extract sparse keyframes

No authentication secrets are printed or copied into output.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


def run(cmd, cwd: Path, check=True, capture=True):
    p = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()
        raise RuntimeError(err[-5000:] if err else f"Command failed: {cmd[0]}")
    return p


def need(binary: str) -> bool:
    return shutil.which(binary) is not None


def safe_name(text: str) -> str:
    text = re.sub(r'[\\/:*?"<>|]+', "_", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:100] or "video"


def timestamp_to_seconds(ts: str) -> float:
    parts = ts.replace(",", ".").split(":")
    try:
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        if len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
    except Exception:
        pass
    return 0.0


def fmt_time(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def subtitle_to_text(path: Path) -> str:
    """Convert VTT/SRT-ish subtitles to compact timestamped text."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    raw = re.sub(r"<[^>]+>", "", raw)
    lines = raw.splitlines()
    out = []
    last_text = None
    current_ts = None

    ts_re = re.compile(
        r"(?P<a>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})\s*-->\s*"
        r"(?P<b>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})"
    )

    for line in lines:
        line = line.strip()
        if not line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        m = ts_re.search(line)
        if m:
            current_ts = fmt_time(timestamp_to_seconds(m.group("a")))
            continue
        if re.fullmatch(r"\d+", line):
            continue
        # Strip VTT positioning/cue artifacts
        line = re.sub(r"align:\S+|position:\S+|size:\S+|line:\S+", "", line).strip()
        if not line:
            continue
        if line == last_text:
            continue
        # Auto captions often append previous line; suppress obvious duplicates.
        if last_text and line.startswith(last_text) and len(line) < len(last_text) + 120:
            if out:
                out[-1] = f"[{current_ts}] {line}" if current_ts else line
            last_text = line
            continue
        out.append(f"[{current_ts}] {line}" if current_ts else line)
        last_text = line
    return "\n".join(out).strip() + "\n"


def find_best_subtitle(folder: Path) -> Optional[Path]:
    candidates = []
    for ext in ("*.vtt", "*.srt"):
        candidates.extend(folder.glob(ext))
    # Prefer Chinese, then English, then anything; avoid live_chat.
    def rank(p: Path):
        n = p.name.lower()
        if "live_chat" in n:
            return (99, len(n))
        if any(x in n for x in (".zh-hans", ".zh-cn", ".zh.")):
            return (0, len(n))
        if any(x in n for x in (".zh-hant", ".zh-tw")):
            return (1, len(n))
        if ".en" in n:
            return (2, len(n))
        return (5, len(n))
    candidates = sorted(candidates, key=rank)
    return candidates[0] if candidates and rank(candidates[0])[0] < 99 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--output", default=".video-digest")
    ap.add_argument("--cookies-from-browser", default=None)
    ap.add_argument("--frames", choices=["off", "auto", "on"], default="off")
    ap.add_argument("--frame-interval", type=int, default=60,
                    help="seconds between frames when extraction is enabled")
    ap.add_argument("--whisper-model", default="small",
                    help="local Whisper model used only when subtitles are unavailable")
    args = ap.parse_args()

    base = Path(args.output).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)

    report = {
        "url": args.url,
        "metadata": "pending",
        "subtitles": "pending",
        "transcription": "not_needed",
        "frames": "not_requested",
        "warnings": [],
    }

    if not need("yt-dlp"):
        report["metadata"] = "failed"
        report["warnings"].append("Missing dependency: yt-dlp")
        (base / "ingest-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("ERROR: yt-dlp is not installed", file=sys.stderr)
        return 2

    common = ["yt-dlp", "--no-playlist"]
    if args.cookies_from_browser:
        common += ["--cookies-from-browser", args.cookies_from_browser]

    # Metadata
    try:
        p = run(common + ["-J", args.url], base)
        meta = json.loads(p.stdout)
        report["metadata"] = "ok"
    except Exception as e:
        report["metadata"] = "failed"
        report["warnings"].append(f"Metadata failed: {e}")
        (base / "ingest-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"ERROR: metadata failed: {e}", file=sys.stderr)
        return 3

    vid = str(meta.get("id") or "video")
    title = safe_name(str(meta.get("title") or vid))
    work = base / f"{title} [{vid}]"
    work.mkdir(parents=True, exist_ok=True)
    (work / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # Subtitles first. Ask yt-dlp for preferred languages, but allow fallback.
    sub_cmd = common + [
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", "zh-Hans,zh-CN,zh,zh-Hant,zh-TW,en.*,en",
        "--sub-format", "vtt/srt/best",
        "-o", "%(title)s [%(id)s].%(ext)s",
        args.url,
    ]
    try:
        run(sub_cmd, work, check=False)
        sub = find_best_subtitle(work)
    except Exception:
        sub = None

    transcript_path = work / "transcript.txt"
    if sub:
        try:
            transcript_path.write_text(subtitle_to_text(sub), encoding="utf-8")
            report["subtitles"] = f"ok:{sub.name}"
            report["transcript_source"] = "subtitle"
        except Exception as e:
            report["subtitles"] = "failed_to_parse"
            report["warnings"].append(f"Subtitle parse failed: {e}")
            sub = None
    else:
        report["subtitles"] = "unavailable"

    # Audio + Whisper only if subtitles failed.
    if not sub:
        if not need("ffmpeg"):
            report["transcription"] = "failed"
            report["warnings"].append("No usable subtitles and ffmpeg is missing.")
        elif not need("whisper"):
            report["transcription"] = "failed"
            report["warnings"].append(
                "No usable subtitles and Whisper CLI is missing. "
                "Install with: pip install -U openai-whisper"
            )
        else:
            audio_tpl = str(work / "audio.%(ext)s")
            audio_cmd = common + [
                "-f", "bestaudio/best",
                "-x", "--audio-format", "mp3",
                "--audio-quality", "5",
                "-o", audio_tpl,
                args.url,
            ]
            try:
                run(audio_cmd, work)
                audios = sorted(work.glob("audio.*"))
                if not audios:
                    raise RuntimeError("yt-dlp finished but no audio file was found")
                audio = audios[0]
                whisper_cmd = [
                    "whisper", str(audio),
                    "--model", args.whisper_model,
                    "--output_dir", str(work),
                    "--output_format", "vtt",
                    "--task", "transcribe",
                ]
                run(whisper_cmd, work, capture=False)
                generated = sorted(work.glob("audio*.vtt"))
                if not generated:
                    raise RuntimeError("Whisper finished but no VTT transcript was found")
                transcript_path.write_text(subtitle_to_text(generated[0]), encoding="utf-8")
                report["transcription"] = f"ok:{args.whisper_model}"
                report["transcript_source"] = "whisper"
            except Exception as e:
                report["transcription"] = "failed"
                report["warnings"].append(f"Audio/transcription failed: {e}")

    # Sparse frame extraction only on explicit request.
    if args.frames in ("auto", "on"):
        if not need("ffmpeg"):
            report["frames"] = "failed"
            report["warnings"].append("Frame extraction requested but ffmpeg is missing.")
        else:
            frames_dir = work / "frames"
            frames_dir.mkdir(exist_ok=True)
            video_tpl = str(work / "visual.%(ext)s")
            try:
                # Low-ish resolution is enough for agent inspection and saves bandwidth.
                run(common + [
                    "-f", "bestvideo[height<=720]/best[height<=720]",
                    "-o", video_tpl,
                    args.url,
                ], work)
                videos = [p for p in work.glob("visual.*") if p.is_file()]
                if not videos:
                    raise RuntimeError("No visual stream downloaded")
                video = videos[0]
                run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-i", str(video),
                    "-vf", f"fps=1/{max(10,args.frame_interval)},scale='min(1280,iw)':-2",
                    "-q:v", "3",
                    str(frames_dir / "frame-%05d.jpg"),
                ], work, capture=False)
                report["frames"] = f"ok:interval={max(10,args.frame_interval)}s"
            except Exception as e:
                report["frames"] = "failed"
                report["warnings"].append(f"Frame extraction failed: {e}")

    # Compact metadata useful to the model.
    summary_meta = {
        "title": meta.get("title"),
        "id": meta.get("id"),
        "webpage_url": meta.get("webpage_url") or args.url,
        "uploader": meta.get("uploader") or meta.get("channel"),
        "duration_seconds": meta.get("duration"),
        "upload_date": meta.get("upload_date"),
        "description": meta.get("description"),
        "view_count": meta.get("view_count"),
    }
    (work / "metadata-summary.json").write_text(
        json.dumps(summary_meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report["work_dir"] = str(work)
    report_path = work / "ingest-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "status": "ok" if transcript_path.exists() else "partial",
        "work_dir": str(work),
        "transcript": str(transcript_path) if transcript_path.exists() else None,
        "report": str(report_path),
    }, ensure_ascii=False, indent=2))
    return 0 if transcript_path.exists() else 4


if __name__ == "__main__":
    raise SystemExit(main())
