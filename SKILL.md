---
name: video-digest
description: Summarize one public, captioned YouTube video from its URL into an evidence-grounded digest with real timestamps, key points, information-density and worth-watching scores, a watch/skip recommendation, and estimated time saved. Use when the user asks to summarize, skim, extract key points or timestamps, focus on one topic, or decide whether a YouTube video is worth watching. Tries a current connected-Chrome transcript capture, the lightweight YouTube transcript interface, then targeted yt-dlp captions, with structured diagnostics and validated local caching. Current T2 does not support Bilibili, channels, multiple videos, local files, speech-to-text fallback, private videos, or visual-only analysis.
---

# Video Digest

Fetch one compact evidence bundle, then summarize only that evidence.

## Fetch evidence

1. Treat the directory containing this `SKILL.md` as `<skill-directory>`.
2. Create a unique temporary `.json` path for this run's evidence output.
3. If a connected Chrome session is available and the target page exposes YouTube's visible transcript, read [references/chrome-transcript.md](references/chrome-transcript.md), create a unique current-run capture, and retain its capture ID. Use only page-visible state; never inspect cookies, local storage, profiles, passwords, tokens, or browser databases.
4. Run from any working directory. Without a Chrome capture:

```powershell
$evidencePath = Join-Path ([IO.Path]::GetTempPath()) ("video-digest-" + [guid]::NewGuid() + ".json")
uv run --directory "<skill-directory>" python -m video_digest "<youtube-url>" --output $evidencePath
```

With a current Chrome capture, append both validation arguments:

```powershell
uv run --directory "<skill-directory>" python -m video_digest "<youtube-url>" --output $evidencePath --chrome-transcript $chromeCapturePath --chrome-capture-id $chromeCaptureId
```

Add `--focus "<question-or-topic>"` for a requested topic. Add repeated `--language <code>` arguments only for an explicit subtitle-language request; otherwise keep Simplified Chinese, Chinese, then English. Add `--no-cache` when the user requests a forced refresh or no persistent transcript cache.

5. Read the UTF-8 JSON. The source order is `chrome_transcript`, `youtube_transcript_api`, then `yt_dlp`. A locally valid cache candidate is used only after a current source confirms the same subtitle track and content version; a current failure never promotes an old transcript to success. Inspect `run.attempts`, `run.artifacts`, and `run.cache` rather than inferring success from files elsewhere.
6. Use only `evidence.metadata` and `evidence.segments` as source material. Verify `evidence.media_downloaded` is `false`; stop and report a product defect if it is ever `true`.

The workflow enumerates caption tracks and fetches exactly one selected track. It prefers a requested-language manual track, then an original/manual track, then one automatic track. Language aliases such as `zh-Hans` and `zh-CN` match. It never uses broad subtitle expressions that expand automatic translations.

## Handle partial results

If `status` is not `complete` or `evidence.failure` is non-null:

- Report each `run.attempts` entry in order, including `source`, `stage`, `code`, actionable `message`, `retryable`, `exit_status`, and redacted `stderr_summary` when present.
- Do not generate a confident summary from empty or partial segments.
- Treat `rate_limited`, `authentication_required`, `site_blocked`, `timeout`, parse failures, and `dependency_missing` as distinct causes.
- For `dependency_missing`, run `uv sync --directory "<skill-directory>"` once and retry.
- For `rate_limited` or `timeout`, do not retry more than once in the same task.
- For `authentication_required`, use a connected Chrome page only when the user can already access the video; do not request pasted credentials.
- Treat `cache_invalid`, `cache_changed`, and `cache_unverified` as cache rejections, not transcript evidence. A `cache_hit` is valid only when the run also records a successful current-source revalidation.

Never include Cookie values, authorization headers, API keys, signed URLs, tokens, or browser database contents in captures, evidence, logs, or the digest.

## Build the digest

Read the complete transcript. For a long transcript, divide it into natural timestamp ranges, summarize each range into claims plus evidence, then merge and deduplicate. Re-open relevant segments before making a precise or contentious claim.

Default to the user's language and this compact structure unless requested otherwise:

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
- 字幕来源、语言、人工或自动字幕、缓存状态，以及任何失败或不确定性
```

Include only decision-relevant information. Collapse introductions, repetition, sponsor copy, rhetorical padding, and outros unless they affect the conclusion.

Use timestamps only from transcript segments. Build timestamp links from `evidence.metadata.canonical_url` plus `&t=<whole-seconds>s`. Never invent timestamps or imply transcript-only evidence includes visual evidence.

Score information density by unique decision-relevant information per minute, not speaking speed or popularity. Choose one recommendation:

- `完整观看`: most of the runtime is uniquely valuable or visual context is essential.
- `只看指定片段`: a few ranges contain most of the value.
- `看摘要即可`: the transcript supports the useful conclusions without watching.
- `跳过`: evidence is repetitive, weak, irrelevant, or too incomplete to justify the time.

Estimate time saved from full duration minus recommended watch segments, rounded to a practical whole minute. If duration is unavailable, say the estimate cannot be calculated reliably.

Separate transcript evidence from inference. Label opinions, predictions, anecdotes, and promotional claims when relevant. If the user explicitly asks for fact-checking, verify only load-bearing claims after producing the transcript-grounded digest, and keep external verification separate from what the video says.

## Finish safely

Delete only the exact temporary evidence and Chrome-capture files created for this run after the final digest is prepared, unless the user asks to keep them. Do not delete the validated local cache or unrelated files.

If the request is for Bilibili, ASR, visual analysis, multiple videos, or channel monitoring, state that the capability belongs to a later ticket instead of pretending T2 supports it.
