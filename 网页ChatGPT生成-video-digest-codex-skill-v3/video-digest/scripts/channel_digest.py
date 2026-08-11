#!/usr/bin/env python3
"""
Discover recent uploads from configured YouTube/Bilibili channels.

This script does NOT summarize videos. It creates a deduplicated queue for the
video-digest skill. Detailed transcription/summarization remains the job of
video_ingest.py + Codex.

Config format: references/channels.example.json
"""

from __future__ import annotations
import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


def run(cmd, cwd: Path):
    p = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return p


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def ymd_to_date(value):
    if not value:
        return None
    value = str(value)
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def normalize_entry(entry, channel):
    url = entry.get("webpage_url") or entry.get("url")
    # Flat playlist entries may expose only an ID; reconstruct common URLs.
    vid = str(entry.get("id") or "")
    if url and url.startswith("http"):
        full_url = url
    else:
        source = channel.get("url", "")
        host = urlparse(source).netloc.lower()
        if "youtube" in host or "youtu.be" in host:
            full_url = f"https://www.youtube.com/watch?v={vid}" if vid else None
        elif "bilibili" in host and vid:
            full_url = f"https://www.bilibili.com/video/{vid}"
        else:
            full_url = url if url and url.startswith("http") else None

    return {
        "id": vid or full_url,
        "title": entry.get("title"),
        "url": full_url,
        "upload_date": entry.get("upload_date"),
        "timestamp": entry.get("timestamp"),
        "duration": entry.get("duration"),
        "channel": channel.get("name") or entry.get("channel") or entry.get("uploader"),
        "channel_url": channel.get("url"),
        "priority": channel.get("priority", "normal"),
        "topics": channel.get("topics", []),
        "description": entry.get("description"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--output", default=".video-digest/channel-digest")
    ap.add_argument("--cookies-from-browser", default=None)
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--max-videos-per-channel", type=int, default=None)
    ap.add_argument("--include-seen", action="store_true")
    args = ap.parse_args()

    if not shutil.which("yt-dlp"):
        print("ERROR: yt-dlp is not installed", file=sys.stderr)
        return 2

    config_path = Path(args.config).expanduser().resolve()
    cfg = load_json(config_path, {})
    channels = cfg.get("channels") or []
    if not channels:
        print("ERROR: config contains no channels", file=sys.stderr)
        return 3

    days = args.days if args.days is not None else int(cfg.get("days", 7))
    max_per = args.max_videos_per_channel if args.max_videos_per_channel is not None else int(cfg.get("max_videos_per_channel", 5))

    out = Path(args.output).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "state.json"
    state = load_json(state_path, {"seen_ids": []})
    seen = set(str(x) for x in state.get("seen_ids", []))

    today = dt.date.today()
    cutoff = today - dt.timedelta(days=max(0, days))

    manifest = []
    report = {"config": str(config_path), "days": days, "channels": [], "warnings": []}

    for ch in channels:
        name = ch.get("name") or ch.get("url")
        url = ch.get("url")
        if not url:
            report["channels"].append({"name": name, "status": "skipped", "reason": "missing url"})
            continue

        cmd = ["yt-dlp", "--flat-playlist", "--dump-single-json", "--playlist-end", str(max_per), url]
        if args.cookies_from_browser:
            cmd[1:1] = ["--cookies-from-browser", args.cookies_from_browser]

        p = run(cmd, out)
        if p.returncode != 0:
            report["channels"].append({
                "name": name,
                "status": "failed",
                "reason": (p.stderr or p.stdout or "")[-2500:]
            })
            continue

        try:
            data = json.loads(p.stdout)
        except Exception as e:
            report["channels"].append({"name": name, "status": "failed", "reason": f"invalid JSON: {e}"})
            continue

        entries = data.get("entries") or []
        added = 0
        for raw in entries:
            item = normalize_entry(raw, ch)
            if not item["id"] or not item["url"]:
                continue

            upload = ymd_to_date(item.get("upload_date"))
            # Some flat playlist outputs omit upload_date. Keep them; the detailed
            # ingest stage can resolve metadata. Strict date filtering only when known.
            if upload and upload < cutoff:
                continue

            if not args.include_seen and str(item["id"]) in seen:
                continue

            manifest.append(item)
            added += 1

        report["channels"].append({"name": name, "status": "ok", "discovered": added})

    # Deduplicate while preserving config/channel order.
    dedup = []
    keys = set()
    for item in manifest:
        key = str(item["id"])
        if key in keys:
            continue
        keys.add(key)
        dedup.append(item)

    (out / "manifest.json").write_text(
        json.dumps(dedup, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "queue.txt").write_text(
        "\n".join(x["url"] for x in dedup if x.get("url")) + ("\n" if dedup else ""),
        encoding="utf-8"
    )
    (out / "discovery-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Mark discovery as seen so subsequent runs only surface newer IDs.
    # Users can delete state.json or use --include-seen to reprocess.
    seen.update(str(x["id"]) for x in dedup)
    state["seen_ids"] = sorted(seen)
    state["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "status": "ok",
        "new_videos": len(dedup),
        "manifest": str(out / "manifest.json"),
        "queue": str(out / "queue.txt"),
        "report": str(out / "discovery-report.json"),
        "state": str(state_path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
