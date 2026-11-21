"""Escape LinkedIn ``little`` commentary text.

Reserved characters must be escaped or the Posts API truncates or rejects
the post. Hashtags written as ``#Word`` stay hashtags.

See https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/little-text-format
"""

from __future__ import annotations

import re

# little Text reserved characters: | { } @ [ ] ( ) < > # \ * _ ~
RESERVED_CHARS = set("\\|{}@[]()<>#*_~")
_HASHTAG = re.compile(r"#[A-Za-z][A-Za-z0-9]*")
COMMENTARY_LIMIT = 3000


def escape_little(text: str) -> str:
    """Escape reserved characters while preserving ``#Hashtag`` tokens."""
    parts: list[str] = []
    position = 0
    for match in _HASHTAG.finditer(text):
        start = match.start()
        if start != 0 and not text[start - 1].isspace():
            continue
        parts.append(_escape_plain(text[position:start]))
        parts.append(match.group(0))
        position = match.end()
    parts.append(_escape_plain(text[position:]))
    return "".join(parts)


def _escape_plain(text: str) -> str:
    escaped: list[str] = []
    for char in text:
        if char in RESERVED_CHARS:
            escaped.append("\\" + char)
        else:
            escaped.append(char)
    return "".join(escaped)
