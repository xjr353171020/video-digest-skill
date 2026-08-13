# Current-run Bilibili subtitle capture

Read this only when a connected Chrome session can access the requested Bilibili BV video and its native subtitle response.

## Safety boundary

- Use the current Bilibili video page and the browser's already authenticated requests.
- Inspect only JSON response bodies for the current video's public view metadata, player subtitle-track metadata, and the single selected subtitle body.
- Do not inspect cookies, local storage, profiles, passwords, session stores, request headers, request cookies, response headers, or browser databases.
- Do not copy or decrypt a Cookie database, even when another process has locked it.
- Do not put Cookie values, tokens, authorization data, signed subtitle URLs, request URLs, response headers, or raw page HTML into the capture.
- Retain only the safe fields listed in the schema below. Discard subtitle download URLs immediately after the browser fetch completes.

## Capture procedure

1. Generate a unique capture ID and unique temporary `.json` path.
2. Parse the requested BV ID and `p` value. Default `p` to `1` and preserve it as part of the video identity.
3. From the current page's Bilibili view metadata response, retain only:
   - `bvid`
   - the selected page's `page`, `cid`, `part`, and `duration`
   - the uploader name
4. From the current page's player subtitle metadata response, select exactly one Chinese track. Prefer a manual requested-language track, then a manual Chinese track, then one `ai-zh` track. Retain only the track language, label, and whether it is generated. Use the signed subtitle URL only inside the browser to fetch that one body; never write the URL to disk or output it.
5. From the selected subtitle body, retain every non-empty row's `from`, `to`, and `content`. Convert them to `start_seconds`, `end_seconds`, and `text`. Keep real subtitle gaps; do not stretch rows to manufacture continuity.
6. Confirm the first and last available subtitle rows were collected, the rows are ordered, the selected CID belongs to the requested `p`, and the last row plausibly reaches the selected part's duration. Only then set `transcript_complete` to `true`.
7. Record the capture time in UTC ISO 8601 form and write this schema:

```json
{
  "schema_version": 2,
  "platform": "bilibili",
  "capture_id": "unique-current-run-id",
  "captured_at": "2026-08-13T12:00:00+00:00",
  "bvid": "BV1X7411F744",
  "page": 5,
  "cid": "156365564",
  "transcript_complete": true,
  "metadata": {
    "title": "Lecture 05 Rasterization 1 (Triangles)",
    "channel": "GAMES-Webinar",
    "duration_seconds": 3974
  },
  "track": {
    "language_code": "zh",
    "is_generated": true,
    "label": "中文"
  },
  "segments": [
    {
      "start_seconds": 5.04,
      "end_seconds": 6.06,
      "text": "第一段字幕"
    }
  ]
}
```

8. Pass the same capture ID through `--chrome-capture-id` and the file through `--chrome-transcript`.

The local validator rejects captures older than 15 minutes, oversized files, malformed JSON, mismatched platform/BV/part/capture IDs, invalid timing, implausible duration coverage, and any sensitive-looking field name anywhere in the document. A rejected or incomplete capture is only a failed attempt; it never becomes summary evidence or a cache entry.
