# Current-run Chrome transcript capture

Read this only when a connected Chrome session can expose YouTube's visible transcript for the requested video.

## Safety boundary

- Use the current page and YouTube's visible **Show transcript** interface.
- Do not inspect cookies, local storage, profiles, passwords, session stores, request headers, or browser databases.
- Do not put tokens, signed URLs, cookies, authorization data, or raw page HTML into the capture.
- If the transcript is unavailable or sign-in is required, skip this capture and let the local workflow record the fallback attempts.

## Capture procedure

1. Generate a unique capture ID and unique temporary `.json` path.
2. Confirm the page video ID matches the requested URL.
3. Open the visible transcript, load it from the first available row through the last available row, and collect every timestamped row. Do not set the completeness marker until both ends have been checked.
4. Use each row's displayed timestamp as its start. Use the next displayed timestamp as the preceding row's end, so adjacent rows are continuous. Use the exact player duration for the last end. If exact duration is unavailable, skip the Chrome capture and let the local sources run; do not claim a complete browser transcript.
5. Record the capture time in UTC ISO 8601 form and write this schema:

```json
{
  "schema_version": 2,
  "capture_id": "unique-current-run-id",
  "captured_at": "2026-08-11T12:00:00+00:00",
  "video_id": "example123",
  "transcript_complete": true,
  "metadata": {
    "title": "Visible title",
    "channel": "Visible channel",
    "duration_seconds": 35
  },
  "track": {
    "language_code": "zh-CN",
    "is_generated": false
  },
  "segments": [
    {
      "start_seconds": 0.0,
      "end_seconds": 20.0,
      "text": "First visible transcript row"
    },
    {
      "start_seconds": 20.0,
      "end_seconds": 35.0,
      "text": "Last visible transcript row"
    }
  ]
}
```

6. Pass the same capture ID through `--chrome-capture-id` and the file through `--chrome-transcript`.

Set `transcript_complete` to `true` only after the first and last available transcript rows have both been collected. The local validator requires exact video duration, plausible start/end coverage, ordered rows, continuity between each row's end and the next row's start, and no synthetic row spanning more than 120 seconds. A short excerpt, disconnected endpoints, or a few rows stretched across a long video cannot pass as complete. The validator rejects captures older than 15 minutes, mismatched capture/video IDs, missing or invalid timestamps, incomplete coverage, oversized files, malformed JSON, and any sensitive-looking field names. A rejected or incomplete capture is only a failed attempt; it never becomes summary evidence or a cache entry.
