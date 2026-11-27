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
    image_url: str | None = None


def fetch_feed_items(feed_url: str, *, max_items: int = 10) -> list[FeedItem]:
    parsed = feedparser.parse(feed_url)
    return items_from_parsed(parsed, feed_url, max_items=max_items)


def items_from_parsed(parsed: Any, feed_url: str, *, max_items: int = 10) -> list[FeedItem]:
    """Normalize a ``feedparser`` result into feed items.

    ``image_url`` is optional metadata. Callers that only read title, link,
    summary, and guid behave as before.
    """
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
                image_url=extract_image_url(entry),
            )
        )
    return items


def extract_image_url(entry: Any) -> str | None:
    """Return the first image URL on a feed entry, if one is present."""
    for enclosure in entry.get("enclosures") or []:
        href = enclosure.get("href") or enclosure.get("url")
        type_ = str(enclosure.get("type") or "")
        if href and type_.startswith("image/"):
            return str(href)
    for media in entry.get("media_content") or []:
        url = media.get("url")
        medium = str(media.get("medium") or "")
        type_ = str(media.get("type") or "")
        if url and (medium == "image" or type_.startswith("image/")):
            return str(url)
    for thumb in entry.get("media_thumbnail") or []:
        url = thumb.get("url")
        if url:
            return str(url)
    for link in entry.get("links") or []:
        if link.get("rel") == "enclosure" and str(link.get("type") or "").startswith("image/"):
            href = link.get("href")
            if href:
                return str(href)
    return None
