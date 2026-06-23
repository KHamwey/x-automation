"""RSS feed polling and item normalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import feedparser


@dataclass
class FeedItem:
    title: str
    link: str | None
    summary: str
    guid: str
    feed_url: str


def fetch_feed_items(feed_url: str, *, max_items: int = 10) -> list[FeedItem]:
    parsed = feedparser.parse(feed_url)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        exc = getattr(parsed, "bozo_exception", None)
        raise RuntimeError(f"Failed to parse feed {feed_url}: {exc}")

    items: list[FeedItem] = []
    for entry in parsed.entries[:max_items]:
        guid = (
            entry.get("id")
            or entry.get("guid")
            or entry.get("link")
            or entry.get("title")
        )
        if not guid:
            continue
        items.append(
            FeedItem(
                title=(entry.get("title") or "").strip(),
                link=(entry.get("link") or "").strip() or None,
                summary=(entry.get("summary") or entry.get("description") or "").strip(),
                guid=str(guid),
                feed_url=feed_url,
            )
        )
    return items
