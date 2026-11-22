"""Local draft queue for approve-first posting."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from x_automation.config import (
    DRAFTS_PATH,
    POST_COST_TEXT,
    POST_COST_WITH_URL,
    ensure_data_dir,
)
from x_automation.filters import normalize_text

DraftStatus = Literal["pending", "approved", "rejected", "published"]
DraftSource = Literal["rss+grok", "cursor", "manual", "rss"]

_URL_PATTERN = re.compile(r"https?://", re.IGNORECASE)
_HASHTAG_BODY = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,29}")
_LEADING_MENTIONS = re.compile(r"^(?:@[A-Za-z0-9_]+\s*)+")
_HANDLE = re.compile(r"^@[A-Za-z0-9_]{1,20}$")
_GENERATED_HASHTAG = re.compile(r"(?<!\w)#[A-Za-z0-9_]+")
_GENERATED_MENTION = re.compile(r"(?<!\w)@[A-Za-z0-9_]+")
_URL_TAIL = re.compile(r"https?://\S*$", re.IGNORECASE)

# Story tags should name the subject. These are category labels and other
# tags that do not identify a particular story.
GENERIC_HASHTAGS = {
    "news",
    "headlines",
    "breaking",
    "breakingnews",
    "comedy",
    "jokes",
    "funny",
    "motocross",
    "mx",
    "sports",
    "sport",
    "update",
    "updates",
    "viral",
    "trending",
    "politics",
    "lol",
    "humor",
    "humour",
    "follow",
    "fyp",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def estimate_post_cost(text: str) -> float:
    if _URL_PATTERN.search(text):
        return POST_COST_WITH_URL
    return POST_COST_TEXT


def ensure_tag_prefix(text: str, tag_emoji: str) -> str:
    normalized_tag = normalize_text(tag_emoji.strip())
    normalized_text = normalize_text(text.strip())
    if normalized_text.startswith(normalized_tag):
        return normalized_text
    return f"{normalized_tag} {normalized_text}"


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_hashtag(raw: str) -> str | None:
    """Return ``#Tag`` or None when the token is empty or not a hashtag."""
    token = re.sub(r"[\s._\-]+", "", str(raw).strip().lstrip("#"))
    if not _HASHTAG_BODY.fullmatch(token):
        return None
    return f"#{token}"


def select_hashtags(
    topic: list[str] | None,
    static: list[str] | None = None,
    *,
    keep_static: int = 0,
    max_topic: int = 2,
    max_total: int = 3,
) -> list[str]:
    """Choose story hashtags, then at most one static category tag.

    Generic topic tags (``#News``, ``#Breaking``, and the like) are dropped.
    ``keep_static`` is clamped to 0 or 1 and ``max_topic`` to 2, so a post
    keeps about 2-3 hashtags. Specific tags come first so a tight character
    budget drops the category tag before the story tag.
    """
    keep = max(0, min(1, _as_int(keep_static, 0)))
    topic_limit = max(0, min(2, _as_int(max_topic, 2)))
    total_limit = max(0, min(3, _as_int(max_total, 3)))

    chosen: list[str] = []
    seen: set[str] = set()

    def add(token: str | None, *, allow_generic: bool) -> bool:
        if token is None or len(chosen) >= total_limit:
            return False
        key = token[1:].lower()
        if key in seen or (not allow_generic and key in GENERIC_HASHTAGS):
            return False
        seen.add(key)
        chosen.append(token)
        return True

    added = 0
    for raw in topic or []:
        if added >= topic_limit or len(chosen) >= total_limit:
            break
        if add(normalize_hashtag(str(raw)), allow_generic=False):
            added += 1

    kept = 0
    for raw in static or []:
        if kept >= keep or len(chosen) >= total_limit:
            break
        if add(normalize_hashtag(str(raw)), allow_generic=True):
            kept += 1
    return chosen


def _prepare_hashtags(body: str, hashtags: list[str] | None) -> list[str]:
    tags: list[str] = []
    existing = {token.lower() for token in body.split()}
    seen: set[str] = set()
    for raw in hashtags or []:
        token = str(raw).strip()
        if not token:
            continue
        if not token.startswith("#"):
            token = f"#{token.lstrip('#')}"
        key = token.lower()
        if key in existing or key in seen:
            continue
        seen.add(key)
        tags.append(token)
    return tags


def _prepare_mentions(
    body: str,
    mentions: list[str] | None,
    *,
    max_mentions: int = 2,
) -> list[str]:
    existing = {token.lower() for token in body.split()}
    seen: set[str] = set()
    tokens: list[str] = []
    cap = max(0, min(2, _as_int(max_mentions, 2)))
    for raw in mentions or []:
        if len(tokens) >= cap:
            break
        token = str(raw).strip()
        if not token:
            continue
        if not token.startswith("@"):
            token = "@" + token.lstrip("@")
        if not _HANDLE.fullmatch(token):
            continue
        key = token.lower()
        if key in existing or key in seen:
            continue
        seen.add(key)
        tokens.append(token)
    return tokens


def _fit_suffix(body: str, tokens: list[str], *, limit: int) -> str:
    """Append tokens inside ``limit`` without replacing the post body."""
    if len(body) > limit:
        body = body[: limit - 1].rstrip() + "…"
    if not tokens:
        return body

    fitted: list[str] = []
    for token in tokens:
        suffix = " " + " ".join(fitted + [token])
        if len(body) + len(suffix) <= limit:
            fitted.append(token)
            continue
        # A token that cannot fit beside any body is dropped. Later tokens
        # are still tried. The body itself is never replaced by tags.
        budget = limit - len(suffix)
        if budget < 1:
            continue
        body = body[: budget - 1].rstrip() + "…"
        fitted.append(token)

    if not fitted:
        return body
    return f"{body} {' '.join(fitted)}"


def append_hashtags(text: str, hashtags: list[str] | None, *, limit: int = 280) -> str:
    """Append hashtags, keeping the post inside X's length limit.

    Always preserves the post body (truncated to ``limit`` if needed). Hashtags
    are included in order while they fit; leftover tags are dropped rather than
    replacing the body.
    """
    body = text.strip()
    return _fit_suffix(body, _prepare_hashtags(body, hashtags), limit=limit)


def move_leading_mentions(text: str) -> str:
    """Move a leading @mention off the first character.

    A post that opens with @ is delivered like a reply and reaches fewer
    people. The words stay; a body that is only mentions is cleared.
    """
    stripped = text.lstrip()
    if not stripped.startswith("@"):
        return text
    match = _LEADING_MENTIONS.match(stripped)
    if not match:
        return text
    leading = match.group(0).split()
    rest = stripped[match.end() :].strip()
    if not rest:
        return ""
    return f"{rest} {' '.join(leading)}"


def strip_generated_tags(
    text: str,
    *,
    hashtags: bool = False,
    mentions: bool = False,
) -> str:
    """Remove model-written tags so only selected ones are appended later.

    Fragments inside a URL are left alone so link posts still resolve.
    """

    cleaned = text
    if mentions:
        cleaned = _GENERATED_MENTION.sub("", cleaned)
    if hashtags:

        def drop_hashtag(match: re.Match[str]) -> str:
            if _URL_TAIL.search(cleaned[: match.start()]):
                return match.group(0)
            return ""

        cleaned = _GENERATED_HASHTAG.sub(drop_hashtag, cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.!?;:])", r"\1", cleaned)
    return cleaned.strip()


def compose_post_text(
    text: str,
    tag_emoji: str,
    hashtags: list[str] | None = None,
    mentions: list[str] | None = None,
    *,
    limit: int = 280,
    max_mentions: int = 2,
) -> str:
    """Build the final post: emoji, body, @mentions, then hashtags.

    Mentions are capped and never placed first. Suffixes that do not fit are
    dropped the same way as ``append_hashtags``; they do not replace the body.
    """
    body = move_leading_mentions(ensure_tag_prefix(text, tag_emoji))
    if not body.strip():
        return ""
    mention_tokens = _prepare_mentions(body, mentions, max_mentions=max_mentions)
    tags = _prepare_hashtags(body, hashtags)
    fitted = _fit_suffix(body, [*mention_tokens, *tags], limit=limit)
    if fitted.lstrip().startswith("@"):
        return _fit_suffix(body, tags, limit=limit)
    return fitted


def _load_all() -> list[dict[str, Any]]:
    if not DRAFTS_PATH.exists():
        return []
    with DRAFTS_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    return []


def _save_all(drafts: list[dict[str, Any]]) -> None:
    ensure_data_dir()
    with DRAFTS_PATH.open("w", encoding="utf-8") as f:
        json.dump(drafts, f, indent=2)


def get_draft(draft_id: str) -> dict[str, Any] | None:
    for draft in _load_all():
        if draft.get("id") == draft_id:
            return draft
    return None


def list_drafts(status: DraftStatus | None = None) -> list[dict[str, Any]]:
    drafts = _load_all()
    if status is None:
        return drafts
    return [d for d in drafts if d.get("status") == status]


def add_draft(
    *,
    category: str,
    text: str,
    tag_emoji: str,
    source: DraftSource,
    source_url: str | None = None,
    rss_guid: str | None = None,
    feed_url: str | None = None,
    metadata: dict[str, Any] | None = None,
    status: DraftStatus = "pending",
    hashtags: list[str] | None = None,
    mentions: list[str] | None = None,
) -> dict[str, Any]:
    final_text = compose_post_text(text, tag_emoji, hashtags, mentions)
    draft = {
        "id": str(uuid.uuid4()),
        "status": status,
        "source": source,
        "category": category,
        "tag_emoji": tag_emoji,
        "text": final_text,
        "source_url": source_url,
        "rss_guid": rss_guid,
        "feed_url": feed_url,
        "created_at": _utc_now_iso(),
        "approved_at": None,
        "published_at": None,
        "x_post_id": None,
        "estimated_x_cost_usd": estimate_post_cost(final_text),
        "metadata": metadata or {},
    }
    drafts = _load_all()
    drafts.append(draft)
    _save_all(drafts)
    return draft


def approve_draft(draft_id: str) -> dict[str, Any]:
    drafts = _load_all()
    for draft in drafts:
        if draft.get("id") == draft_id:
            if draft.get("status") == "published":
                raise ValueError(f"Draft {draft_id} is already published.")
            draft["status"] = "approved"
            draft["approved_at"] = _utc_now_iso()
            _save_all(drafts)
            return draft
    raise ValueError(f"Draft not found: {draft_id}")


def reject_draft(draft_id: str) -> dict[str, Any]:
    drafts = _load_all()
    for draft in drafts:
        if draft.get("id") == draft_id:
            if draft.get("status") == "published":
                raise ValueError(f"Draft {draft_id} is already published.")
            draft["status"] = "rejected"
            _save_all(drafts)
            return draft
    raise ValueError(f"Draft not found: {draft_id}")


def mark_published(draft_id: str, x_post_id: str) -> dict[str, Any]:
    drafts = _load_all()
    for draft in drafts:
        if draft.get("id") == draft_id:
            draft["status"] = "published"
            draft["published_at"] = _utc_now_iso()
            draft["x_post_id"] = x_post_id
            _save_all(drafts)
            return draft
    raise ValueError(f"Draft not found: {draft_id}")


def has_rss_draft(feed_url: str, rss_guid: str) -> bool:
    key = f"{feed_url}:{rss_guid}"
    for draft in _load_all():
        if draft.get("feed_url") == feed_url and draft.get("rss_guid") == rss_guid:
            return True
        if draft.get("metadata", {}).get("rss_key") == key:
            return True
    return False
