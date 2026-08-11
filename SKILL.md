---
name: video-digest
description: Summarize one public, captioned YouTube video from its URL into an evidence-grounded digest with real timestamps, key points, information-density and worth-watching scores, a watch/skip recommendation, and estimated time saved. Use when the user asks to summarize, skim, extract key points or timestamps, focus on one topic, or decide whether a YouTube video is worth watching. Current T1 does not support Bilibili, channels, multiple videos, local files, speech-to-text fallback, private videos, or visual-only analysis.
---

# Video Digest

Use the bundled Python workflow to fetch a compact evidence bundle, then summarize only that evidence.

## Fetch evidence

1. Treat the directory containing this `SKILL.md` as `<skill-directory>`.
2. Create a unique temporary `.json` path for this run.
3. Run the command from any working directory:

```powershell
$evidencePath = Join-Path ([IO.Path]::GetTempPath()) ("video-digest-" + [guid]::NewGuid() + ".json")
uv run --directory "<skill-directory>" python -m video_digest "<youtube-url>" --output $evidencePath
```

Add `--focus "<question-or-topic>"` when the user asks about a particular topic. Add repeated `--language <code>` arguments only when the user requests a specific subtitle language; otherwise keep the built-in preference order of Simplified Chinese, Chinese, then English.

4. Read the UTF-8 JSON file. Use only `evidence.metadata` and `evidence.segments` as source material for the digest.
5. Verify that `evidence.media_downloaded` is `false`. Stop and report a product defect if it is ever `true`.

The command uses a single selected subtitle track and `yt-dlp --skip-download`. It may create one temporary JSON3 subtitle inside an isolated temporary directory; it must not retain audio or video.

## Handle partial results

If `status` is not `complete` or `evidence.failure` is non-null:

- Report the exact `stage`, `code`, actionable `message`, and whether retrying may help.
- Do not generate a confident summary when `segments` is empty.
- Do not reinterpret `captions_unavailable` or `caption_empty` as a successful transcript.
- For `dependency_missing`, run `uv sync --directory "<skill-directory>"` once and retry.
- For `rate_limited` or `timeout`, do not retry more than once in the same task.
- For `authentication_required`, explain that private or access-restricted videos are outside T1.

Do not ask the user to paste Cookie, API keys, tokens, or credentials. Do not copy or decrypt a browser Cookie database in T1.

## Build the digest

Read the complete transcript. For a long transcript, divide it into natural timestamp ranges, summarize each range into claims plus evidence, then merge and deduplicate. Re-open the relevant segments before making a precise or contentious claim.

Default to the user's language and use this compact structure unless they request another format:

```markdown
# 视频速读

**标题：**
**作者/频道：**
**时长：**
**一句话结论：**

## 核心信息
1. ...

## 建议看的片段
- 00:00–00:00 — 原因

## 时间价值
- **信息密度：** x/10
- **值得完整观看：** x/10
- **建议：** 完整观看 / 只看指定片段 / 看摘要即可 / 跳过
- **预计节省时间：** 约 xx 分钟

## 证据边界
- 字幕来源、语言、人工或自动字幕，以及任何缺失或不确定性
```

Include only points that materially help the user understand or decide. Collapse introductions, repetition, sponsor copy, rhetorical padding, and outros unless they affect the conclusion.

Use timestamps only from transcript segments. Build timestamp links from `evidence.metadata.canonical_url` plus `&t=<whole-seconds>s`. Never invent a timestamp or imply that a transcript-only result includes visual evidence.

Score information density by unique decision-relevant information per minute, not speaking speed or popularity. Choose one recommendation:

- `完整观看`: most of the runtime is uniquely valuable or visual context is essential.
- `只看指定片段`: a few ranges contain most of the value.
- `看摘要即可`: the transcript supports the useful conclusions without watching.
- `跳过`: evidence is repetitive, weak, irrelevant, or too incomplete to justify the time.

Estimate time saved transparently from the full duration minus the recommended watch segments, rounded to a practical whole-minute estimate.

Separate direct transcript evidence from inference. Label opinions, predictions, anecdotes, and promotional claims when relevant. If the user explicitly asks for fact-checking, verify only load-bearing claims after producing the transcript-grounded digest, and keep external verification separate from what the video says.

## Finish safely

Delete only the exact temporary evidence file created for this run after the final digest is prepared, unless the user asks to keep it. Never delete or modify unrelated files.

If the request is for Bilibili, ASR, visual analysis, multiple videos, or channel monitoring, state that the capability belongs to a later ticket instead of pretending T1 supports it.
