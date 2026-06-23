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
) -> dict[str, Any]:
    final_text = ensure_tag_prefix(text, tag_emoji)
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
