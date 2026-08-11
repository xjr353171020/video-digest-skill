# Setup & usage

## Install

Codex loads personal skills from:

```text
$HOME/.agents/skills/
```

Copy the entire `video-digest` folder there:

```text
~/.agents/skills/video-digest/
├── SKILL.md
├── scripts/
│   └── video_ingest.py
└── references/
    └── SETUP.md
```

Codex usually detects skill changes automatically. If it does not appear, restart Codex.

## Dependencies

### 1. yt-dlp

Recommended:

```bash
python -m pip install -U --pre "yt-dlp[default]"
```

### 2. ffmpeg

Install the actual `ffmpeg` and `ffprobe` binaries with your OS package manager.

### 3. Whisper (only needed when a video has no usable subtitles)

```bash
python -m pip install -U openai-whisper
```

For CPU-only machines, choose a smaller Whisper model:

```bash
python scripts/video_ingest.py "URL" --whisper-model base
```

## Use from Codex

Explicit invocation:

```text
$video-digest 总结这个视频，告诉我是否值得看：
https://www.youtube.com/watch?v=...
```

or:

```text
$video-digest
把这几个 B 站视频都扫一遍，按信息密度排序，只告诉我值得看的：
URL1
URL2
URL3
```

Codex can also invoke the skill implicitly when the request clearly asks for video summarization.

## Logged-in videos

If a video is available to you in your local browser but yt-dlp needs that login state, the script supports:

```bash
python scripts/video_ingest.py "URL" --cookies-from-browser chrome
```

Supported browser names depend on yt-dlp. Common examples include:
`chrome`, `edge`, `firefox`, `brave`, `chromium`, `safari`.

Do not paste cookie values into prompts or store them in this skill.

## Visual-heavy videos

For slides, UI walkthroughs, charts, or tutorials:

```bash
python scripts/video_ingest.py "URL" --frames auto
```

Default frame interval is 60 seconds. Adjust if needed:

```bash
python scripts/video_ingest.py "URL" --frames on --frame-interval 30
```

Avoid enabling frames for ordinary talking-head videos.

## Fact-check mode

The skill can also verify the video's most important factual claims when Codex has web access. Example:

```text
$video-digest 总结这个视频，并核查里面最关键的 5 个事实性说法。告诉我哪些是真的、哪些夸大或误导：
https://www.youtube.com/watch?v=...
```

Recommended behavior is targeted verification, not line-by-line checking. The skill first summarizes the video, extracts a small number of load-bearing claims, and prefers primary sources for verification.


## Channel digest

Copy the example config:

### macOS / Linux

```bash
mkdir -p ~/.config/video-digest
cp references/channels.example.json ~/.config/video-digest/channels.json
```

### Windows PowerShell

```powershell
New-Item -ItemType Directory -Force "$HOME\.config\video-digest"
Copy-Item "references\channels.example.json" "$HOME\.config\video-digest\channels.json"
```

Edit `channels.json` and replace the example URLs with the channels you follow.

Discover only newly seen videos:

```bash
python scripts/channel_digest.py \
  --config ~/.config/video-digest/channels.json \
  --output .video-digest/channel-digest
```

Then ask Codex:

```text
$video-digest
读取 .video-digest/channel-digest/manifest.json，
把新视频按相关性和潜在信息密度先筛一遍。
只深入处理值得看的视频，并输出“本周视频情报”。
```

For logged-in feeds/channels:

```bash
python scripts/channel_digest.py \
  --config ~/.config/video-digest/channels.json \
  --output .video-digest/channel-digest \
  --cookies-from-browser chrome
```

### Reset / reprocess

To re-scan previously seen videos, either:
- use `--include-seen`, or
- delete `.video-digest/channel-digest/state.json`.

`state.json` contains only discovered IDs and timestamps; it should not contain browser cookies or API keys.
