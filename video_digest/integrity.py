from __future__ import annotations

import hashlib
import json

from .domain import EvidenceBundle


def evidence_content_sha256(evidence: EvidenceBundle) -> str:
    payload = {
        "video_id": evidence.metadata.video_id,
        "title": evidence.metadata.title,
        "channel": evidence.metadata.channel,
        "duration_seconds": evidence.metadata.duration_seconds,
        "canonical_url": evidence.metadata.canonical_url,
        "source": evidence.transcript_source,
        "language": evidence.transcript_language,
        "is_generated": evidence.transcript_is_generated,
        "segments": [
            {
                "start": segment.start_seconds,
                "end": segment.end_seconds,
                "text": segment.text,
            }
            for segment in evidence.segments
        ],
    }
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()
