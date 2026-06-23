"""RSS fetch + Grok ingest into the draft queue."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from x_automation.config import FEEDS_PATH, SEEN_RSS_PATH, ensure_data_dir
from x_automation.drafts import add_draft, has_rss_draft
from x_automation.grok import transform_item
from x_automation.rss import FeedItem, fetch_feed_items


@dataclass
class IngestResult:
    feeds_checked: int = 0
    items_seen: int = 0
    drafts_created: int = 0
    skipped_seen: int = 0
    skipped_duplicate: int = 0
    errors: int = 0
    error_details: list[str] = field(default_factory=list)


def load_feeds_config(path: Path | None = None) -> dict[str, Any]:
    feeds_path = path or FEEDS_PATH
    if not feeds_path.exists():
        raise FileNotFoundError(
            f"Feeds config not found: {feeds_path}. Copy feeds.yaml.example to feeds.yaml."
        )
    with feeds_path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_daily_publish_cap() -> int:
    """Resolve daily cap: DRAFTS_DAILY_CAP env > feeds.yaml defaults > 3."""
    from x_automation.config import DEFAULT_DAILY_PUBLISH_CAP, get_env

    env_cap = get_env("DRAFTS_DAILY_CAP")
    if env_cap:
        return int(env_cap)
    try:
        config = load_feeds_config()
        return int(
            (config.get("defaults") or {}).get(
                "daily_publish_cap", DEFAULT_DAILY_PUBLISH_CAP
            )
        )
    except FileNotFoundError:
        return DEFAULT_DAILY_PUBLISH_CAP


def _load_seen() -> set[str]:
    if not SEEN_RSS_PATH.exists():
        return set()
    with SEEN_RSS_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    return set(data) if isinstance(data, list) else set()


def _save_seen(seen: set[str]) -> None:
    ensure_data_dir()
    with SEEN_RSS_PATH.open("w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, indent=2)


def _rss_key(feed_url: str, guid: str) -> str:
    return f"{feed_url}:{guid}"


def run_ingest(*, use_grok: bool = True, feeds_path: Path | None = None) -> IngestResult:
    config = load_feeds_config(feeds_path)
    defaults = config.get("defaults") or {}
    max_drafts = int(defaults.get("max_drafts_per_fetch", 5))
    categories = config.get("categories") or {}

    seen = _load_seen()
    result = IngestResult()
    created = 0

    for category_name, category_cfg in categories.items():
        tag = category_cfg.get("tag", "")
        grok_style = category_cfg.get("grok_style", "news_brief")
        feeds = category_cfg.get("feeds") or []

        for feed_entry in feeds:
            if created >= max_drafts:
                break

            feed_url = feed_entry.get("url")
            if not feed_url:
                continue
            include_url = bool(feed_entry.get("include_url", True))

            result.feeds_checked += 1
            try:
                items = fetch_feed_items(feed_url, max_items=max_drafts)
            except Exception as exc:
                result.errors += 1
                result.error_details.append(f"{feed_url}: {exc}")
                continue

            for item in items:
                if created >= max_drafts:
                    break
                result.items_seen += 1
                key = _rss_key(item.feed_url, item.guid)
                if key in seen:
                    result.skipped_seen += 1
                    continue
                if has_rss_draft(item.feed_url, item.guid):
                    result.skipped_duplicate += 1
                    seen.add(key)
                    continue

                try:
                    text = _item_to_text(
                        item,
                        category=category_name,
                        grok_style=grok_style,
                        include_url=include_url,
                        use_grok=use_grok,
                    )
                    add_draft(
                        category=category_name,
                        text=text,
                        tag_emoji=tag,
                        source="rss+grok" if use_grok else "rss",
                        source_url=item.link,
                        rss_guid=item.guid,
                        feed_url=item.feed_url,
                        metadata={
                            "rss_key": key,
                            "grok_style": grok_style if use_grok else None,
                            "title": item.title,
                        },
                    )
                    seen.add(key)
                    created += 1
                    result.drafts_created += 1
                except Exception as exc:
                    result.errors += 1
                    result.error_details.append(f"{key}: {exc}")

    _save_seen(seen)
    return result


def _item_to_text(
    item: FeedItem,
    *,
    category: str,
    grok_style: str,
    include_url: bool,
    use_grok: bool,
) -> str:
    if use_grok:
        return transform_item(
            style=grok_style,
            category=category,
            title=item.title,
            summary=item.summary,
            link=item.link,
            include_url=include_url,
        )
    text = item.title
    if include_url and item.link:
        text = f"{text} {item.link}"
    return text[:280]


def print_ingest_summary(result: IngestResult) -> None:
    print("\n--- Ingest summary ---")
    print(f"  Feeds checked:     {result.feeds_checked}")
    print(f"  Items seen:        {result.items_seen}")
    print(f"  Drafts created:    {result.drafts_created}")
    print(f"  Skipped (seen):    {result.skipped_seen}")
    print(f"  Skipped (dup):     {result.skipped_duplicate}")
    print(f"  Errors:            {result.errors}")
    if result.error_details:
        for detail in result.error_details[:10]:
            print(f"    - {detail}")
