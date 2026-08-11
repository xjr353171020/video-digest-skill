---
name: video-digest
description: Summarize YouTube, Bilibili, and other yt-dlp-supported videos from URLs or subscribed channels. Use when the user asks to summarize, skim, extract key points/timestamps, judge whether a video is worth watching, compare multiple videos, scan recent uploads from channels, or build a high-signal video digest. Prefer existing subtitles, fall back to local audio transcription, and inspect keyframes only when visual content matters.
---

# Video Digest

Turn a video URL into a high-signal brief without forcing the user to watch the whole video.

## Core principles

1. Prefer text over video:
   - First obtain metadata and creator-provided subtitles.
   - If unavailable, try auto-generated subtitles.
   - Only if no usable subtitles exist, download audio and transcribe it.
   - Do not download the full video unless visual inspection is actually necessary.

2. Separate evidence from interpretation:
   - Distinguish what the speaker explicitly says from your own inference.
   - Do not silently turn opinions, predictions, sponsorship claims, or anecdotes into facts.
   - If important factual claims should be checked and web access is available, verify only the load-bearing claims and clearly mark the result as external verification.

3. Optimize for the user's time:
   - Identify what is genuinely new, actionable, surprising, or decision-relevant.
   - Collapse repetition, filler, intros, outros, sponsor segments, and rhetorical padding.
   - Provide timestamps whenever the transcript makes them available.
   - End with a recommendation: watch all / watch selected segments / summary is enough / skip.

4. Never expose or copy browser cookies, API keys, tokens, or session data into prompts or output.
   `--cookies-from-browser` may be used locally when the user has authorized access.

## Inputs

Typical inputs:
- One YouTube/Bilibili URL
- Multiple video URLs
- A URL plus a requested focus, e.g. "只总结 AI 芯片部分"
- A request such as "值得看吗", "给我省时间", "做成会议式笔记", or "比较这几个视频"

## Workflow

### Step 1 — Ingest

Run:

```bash
python scripts/video_ingest.py "<URL>" --output ".video-digest"
```

If access requires an already logged-in browser and the user has authorized use of local browser cookies:

```bash
python scripts/video_ingest.py "<URL>" --output ".video-digest" --cookies-from-browser chrome
```

Replace `chrome` with the user's actual browser when needed.

The script creates a per-video folder containing:
- `metadata.json`
- `transcript.txt` when subtitles or transcription succeed
- original subtitle files when available
- `audio.*` only when transcription fallback is needed
- `frames/` only when explicitly requested
- `ingest-report.json`

Read `ingest-report.json` first, then `metadata.json`, then `transcript.txt`.

### Step 2 — Decide whether visuals are needed

Default: do not inspect frames.

Visual inspection is useful when:
- the speaker refers to charts, slides, tables, UI, code, diagrams, products, or on-screen comparisons;
- this is a software tutorial or presentation where actions are not described verbally;
- the transcript is incomplete or ambiguous because crucial information is only on screen.

If needed, rerun with:

```bash
python scripts/video_ingest.py "<URL>" --output ".video-digest" --frames auto
```

Then inspect only a small number of representative frames. Do not attempt frame-by-frame viewing unless the task specifically requires it.

### Step 3 — Analyze

For a single informational video, extract:

- One-sentence thesis
- 3–10 core points
- New/useful information
- Important claims and their type:
  - fact/data claim
  - opinion
  - prediction
  - anecdote
  - sponsored/promotional claim
- Action items, if any
- Best timestamps/segments
- Repetition/filler that can be skipped
- Information density score (1–10)
- Worth-watching score (1–10)

When judging information density, consider how much unique decision-relevant information exists relative to runtime. Do not equate speaking speed with information density.

### Step 4 — Output

Default to the user's language.

Use this structure unless they ask for another format:

```markdown
# 视频速读

**标题：**
**作者/频道：**
**时长：**
**一句话结论：**

## 核心信息
1. ...
2. ...
3. ...

## 真正值得注意的新信息
- [时间] ...
- [时间] ...

## 关键论据 / 数据
- ...

## 观点与事实区分
- **事实/数据：** ...
- **作者观点：** ...
- **预测/推测：** ...
- **广告/利益相关：** ...（如可判断）

## 建议看的片段
- 00:00–00:00 — 原因

## 可以跳过
- 00:00–00:00 — 原因

## 时间价值
- **信息密度：** x/10
- **值得完整观看：** x/10
- **建议：** 完整观看 / 只看指定片段 / 看摘要即可 / 跳过
- **预计节省时间：** 约 xx 分钟
```

If timestamps are not available, omit fake timestamps.

For multiple videos, add a comparison table with:
- title
- runtime
- thesis
- information density
- novelty
- recommended action

Then identify contradictions, duplicated talking points, and which single video gives the best time-to-information ratio.


## Optional fact-checking

When web access is available and the user asks whether the video is accurate, credible, misleading, exaggerated, or worth trusting, perform targeted verification after the initial summary.

### What to verify

Verify only claims that materially affect the video's conclusion or the user's decision. Prioritize:
- concrete numbers, dates, prices, benchmarks, market-share claims, scientific findings, laws, policies, product specifications, and quotations;
- claims presented as breaking news or recent developments;
- causal claims that are central to the argument;
- claims where the speaker cites a study, report, government body, company, or named expert;
- claims that appear surprising, controversial, or too precise to accept without checking.

Do not waste time verifying:
- subjective opinions or personal preferences;
- obvious common knowledge;
- harmless rhetorical flourishes;
- every minor sentence in a long video.

### Verification method

1. Extract 3–8 load-bearing claims from the transcript.
2. Search for primary sources first: official documents, original research papers, company filings, regulator/government pages, product documentation, or the original quoted source.
3. Use reputable secondary sources only when primary sources are unavailable or useful for context.
4. Prefer recent sources when the claim is time-sensitive.
5. Compare the claim with the source carefully; do not treat a related source as confirmation.
6. Classify each checked claim as one of:
   - **Supported** — the source materially supports the claim.
   - **Mostly supported** — directionally right but simplified or missing caveats.
   - **Misleading** — technically based on something real but framed in a materially deceptive way.
   - **Unsupported** — no reliable support found.
   - **False** — reliable evidence contradicts the claim.
   - **Unverifiable** — insufficient evidence or the claim is inherently speculative.
7. Cite the source used for each checked claim whenever the environment supports citations or links.

### Fact-check output

Append this section only when verification is requested or clearly useful:

```markdown
## 关键事实核查

| 视频中的说法 | 结论 | 核查结果 |
|---|---|---|
| ... | ✅ Supported | ... |
| ... | ⚠️ Mostly supported | ... |
| ... | ❌ False | ... |

### 可信度判断
- **整体可信度：** x/10
- **主要问题：** ...
- **是否影响视频核心结论：** 是 / 否 / 部分
```

Do not assign a high or low overall credibility score merely because one minor claim was wrong. Weight errors by how much they affect the video's central thesis.

## Long transcripts

If `transcript.txt` is too long to reason over reliably in one pass:

1. Split by natural timestamp ranges or sections, not arbitrary character boundaries when possible.
2. Summarize each chunk into claims + evidence + timestamps.
3. Merge chunk summaries.
4. Deduplicate repeated arguments.
5. Re-open relevant transcript ranges before making precise or contentious claims.

Do not pretend to have read omitted chunks.

## Failure handling

If ingestion fails:
1. Read `ingest-report.json`.
2. Report the exact stage that failed: metadata, subtitles, audio, transcription, or frames.
3. Check whether `yt-dlp`, `ffmpeg`, or `whisper` is missing.
4. If the site requires login, suggest local `--cookies-from-browser`.
5. Do not ask the user to paste cookies or authentication secrets.
6. Do not attempt to defeat DRM or access controls.

## Dependencies

Required:
- Python 3.10+
- `yt-dlp`
- `ffmpeg` / `ffprobe`

Optional fallback transcription:
- OpenAI Whisper CLI (`pip install -U openai-whisper`)

The ingestion script checks these tools and reports missing dependencies.


## Channel / subscription digest

Use this mode when the user wants to monitor or scan several YouTube/Bilibili channels and only surface worthwhile new uploads.

### Config

Create a local config based on:

```text
references/channels.example.json
```

Recommended location for a personal config:

```text
~/.config/video-digest/channels.json
```

Do not commit private cookies or credentials into the config.

Example:

```json
{
  "days": 7,
  "max_videos_per_channel": 5,
  "channels": [
    {
      "name": "Example YouTube",
      "url": "https://www.youtube.com/@example/videos",
      "priority": "high",
      "topics": ["AI", "agents"]
    },
    {
      "name": "Example Bilibili",
      "url": "https://space.bilibili.com/123456/video",
      "priority": "normal",
      "topics": ["科技"]
    }
  ]
}
```

### Discover recent uploads

Run:

```bash
python scripts/channel_digest.py \
  --config "~/.config/video-digest/channels.json" \
  --output ".video-digest/channel-digest"
```

Optional local browser login state:

```bash
python scripts/channel_digest.py \
  --config "~/.config/video-digest/channels.json" \
  --output ".video-digest/channel-digest" \
  --cookies-from-browser chrome
```

The script produces:

- `manifest.json` — normalized list of newly discovered videos
- `state.json` — IDs already seen, used for deduplication
- `queue.txt` — URLs selected for detailed ingestion
- `discovery-report.json` — failures/warnings by channel

### Detailed digest workflow

1. Read `manifest.json`.
2. Remove obviously irrelevant videos using title, description, channel priority, requested topics, and upload date.
3. For the remaining videos, call `scripts/video_ingest.py` per URL.
4. Summarize each video using the normal Video Digest workflow.
5. Verify only load-bearing factual claims when the user requests fact checking or when a claim materially affects the recommendation.
6. Compare videos across channels and deduplicate repeated news/topics.

### Default channel-digest output

Use a compact executive digest:

```markdown
# 今日 / 本周视频情报

## 先看这几个
| 视频 | 频道 | 时长 | 新信息 | 信息密度 | 建议 |
|---|---|---:|---:|---:|---|
| ... | ... | ... | 9/10 | 8/10 | 必看 / 看摘要 / 跳过 |

## 1. 最值得看
### 标题
- **一句话：**
- **为什么值得看：**
- **核心新信息：**
- **建议观看：** 12:40–18:10
- **完整看？** 否

## 2. 看摘要就够
...

## 可以直接跳过
- 视频 A — 原因
- 视频 B — 原因

## 跨视频结论
- 多个频道都在重复的观点：
- 真正新增的信息：
- 有争议/互相矛盾的说法：
- 今天最值得进一步研究的问题：

## 时间账单
- 扫描视频：N 个
- 视频总时长：X 小时
- 建议实际观看：Y 分钟
- 预计节省：Z 小时
```

### Ranking guidance

Rank for the user's time, not for popularity.

Consider:
- novelty: is there information not already repeated by other videos?
- evidence quality: are claims sourced and specific?
- actionability: can the user make a decision or do something with it?
- density: unique useful information per minute
- relevance: does it match the user's configured topics/interests?
- redundancy: does another shorter/better video cover the same material?
- promotional load: is the video mainly sponsorship, hype, or self-promotion?

Do not automatically recommend high-view-count or high-priority channels. High priority means "inspect earlier", not "trust more".

### State and deduplication

`channel_digest.py` tracks previously seen video IDs in `state.json`.

- Do not reprocess old videos unless the user asks.
- If several channels discuss the same event, merge the repeated points and identify which source adds the most original information.
- A re-upload or mirrored video should be treated as duplicate when title/content strongly overlaps.
