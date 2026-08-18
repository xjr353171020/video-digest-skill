from __future__ import annotations

import re


def sanitize_external_diagnostic(output: str) -> str | None:
    """Return a compact diagnostic that is safe to serialize or show to the user."""

    sanitized = _QUOTED_WINDOWS_ABSOLUTE_PATH_PATTERN.sub("<redacted-path>", output)
    sanitized = _WINDOWS_ABSOLUTE_PATH_PATTERN.sub("<redacted-path>", sanitized)
    sanitized = _URL_PATTERN.sub("<redacted-url>", sanitized)
    sanitized = _BEARER_PATTERN.sub(r"\1<redacted>", sanitized)
    sanitized = _BASIC_PATTERN.sub(r"\1<redacted>", sanitized)
    sanitized = _AUTHORIZATION_HEADER_PATTERN.sub(r"\1<redacted>", sanitized)
    sanitized = _COOKIE_HEADER_PATTERN.sub(r"\1<redacted>", sanitized)
    sanitized = _SENSITIVE_FIELD_PATTERN.sub(r"\1<redacted>", sanitized)
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    if not sanitized:
        return None
    return sanitized[:500]


_URL_PATTERN = re.compile(r"https?://[^\s]+", flags=re.IGNORECASE)
_QUOTED_WINDOWS_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?ix)(?:[\"'])[A-Z]:\\[^\"'\r\n]+(?:[\"'])",
)
_WINDOWS_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?ix)\b[A-Z]:\\(?:[^\\\r\n\t]+\\)*[^\\\r\n\t]*?"
    r"(?=\s+(?:cookie|set-cookie|authorization|bearer|basic)\b|[\r\n]|$)",
)
_BEARER_PATTERN = re.compile(
    r"(?i)(\bbearer\s+)[^\s,;}\]]+",
)
_BASIC_PATTERN = re.compile(
    r"(?i)(\bbasic\s+)[A-Za-z0-9+/=_-]+",
)
_AUTHORIZATION_HEADER_PATTERN = re.compile(
    r"(?im)(\b(?:authorization|proxy[-_]authorization)\s*[:=]\s*)[^\r\n]*$",
)
_COOKIE_HEADER_PATTERN = re.compile(
    r"(?im)(\b(?:cookie|set-cookie)\s*:\s*)[^\r\n]*$",
)
_SENSITIVE_FIELD_PATTERN = re.compile(
    r"""(?ix)
    (
        ["']?
        (?:
            authorization
            | proxy[-_]authorization
            | cookie
            | set[-_]?cookie
            | x[-_]?api[-_]?key
            | api[-_]?key
            | access[-_]?token
            | refresh[-_]?token
            | id[-_]?token
            | client[-_]?secret
            | auth[-_]?token
            | secret[-_]?key
            | password
            | secret
            | token
        )
        ["']?
        \s*[:=]\s*
    )
    (
        "(?:\\.|[^"])*"
        | '(?:\\.|[^'])*'
        | [^\s,;}\]]+
    )
    """,
)
