"""Post filtering helpers (emoji tags, tag aliases).

Emoji-based tagging is a client-side convention: prepend a distinctive emoji
(e.g. 🟦) to posts you may want to bulk-delete later. The X API has no native
tag field, so filtering happens after fetch by substring match on ``text``.

Because filtering is client-side, **all pages are still fetched** (and billed
as owned reads) even when ``--emoji`` is set — only the delete target list is
narrowed. For large accounts, tag posts consistently to avoid paying to scan
unrelated history.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

from x_automation.config import TAGS_PATH


def normalize_text(text: str) -> str:
    """Normalize Unicode for consistent emoji matching.

    Emojis can be represented multiple ways in Unicode (e.g. composed vs
    decomposed sequences). NFC normalization prevents false negatives when
    matching ``--emoji`` against tweet text.
    """
    return unicodedata.normalize("NFC", text)


def load_tag_map(path: Path | None = None) -> dict[str, str]:
    """Load emoji -> label mapping from tags.json."""
    tags_path = path or TAGS_PATH
    if not tags_path.exists():
        return {}
    with tags_path.open(encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return {normalize_text(k): v for k, v in data.items()}
    return {}


def resolve_emoji_filter(emoji: str | None, tag: str | None, tags_path: Path | None = None) -> str | None:
    """Resolve --tag alias to emoji, or return --emoji directly."""
    if emoji:
        return normalize_text(emoji)
    if not tag:
        return None
    tag_map = load_tag_map(tags_path)
    label_to_emoji = {v.lower(): k for k, v in tag_map.items()}
    resolved = label_to_emoji.get(tag.lower())
    if not resolved:
        available = ", ".join(tag_map.values()) or "(none — create data/tags.json)"
        raise ValueError(f"Unknown tag '{tag}'. Available tags: {available}")
    return resolved


def post_matches_emoji(post: dict, emoji: str) -> bool:
    """True if the emoji appears anywhere in the post text (substring match)."""
    text = post.get("text") or ""
    return normalize_text(emoji) in normalize_text(text)


def filter_posts(posts: list[dict], emoji: str | None = None) -> list[dict]:
    if not emoji:
        return posts
    return [p for p in posts if post_matches_emoji(p, emoji)]
