"""RSS fetch + Grok ingest into the draft queue."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from x_automation.config import FEEDS_PATH, SEEN_RSS_PATH, ensure_data_dir
from x_automation.drafts import add_draft, has_rss_draft, select_hashtags, strip_generated_tags
from x_automation.grok import Annotations, category_voice_name, suggest_annotations, transform_item
from x_automation.mentions import verify_mentions
from x_automation.rss import FeedItem, fetch_feed_items


@dataclass
class IngestResult:
    feeds_checked: int = 0
    items_seen: int = 0
    drafts_created: int = 0
    skipped_seen: int = 0
    skipped_duplicate: int = 0
    skipped_filtered: int = 0
    errors: int = 0
    error_details: list[str] = field(default_factory=list)
    created_ids: list[str] = field(default_factory=list)


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


def is_skipped_post(text: str) -> bool:
    """Grok replies SKIP when a headline should not become a post."""
    cleaned = text.strip().strip("\"'`").rstrip(".").strip()
    return cleaned.upper() == "SKIP"


@dataclass(frozen=True)
class EngagementSettings:
    """Per-category hashtag and mention options.

    When none of the hashtag keys are set, drafts keep every static tag from
    ``hashtags``. Setting any of those keys switches to the selector: 1-2
    story tags plus 0-1 static tag, at most 3 total.
    """

    topic_hashtags: bool
    use_selector: bool
    keep_static: int
    max_topic: int
    max_total: int
    mentions: bool
    max_mentions: int


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def resolve_engagement(
    category_cfg: dict[str, Any] | None,
    defaults: dict[str, Any] | None = None,
) -> EngagementSettings:
    """Read hashtag and mention settings, with category keys over defaults."""
    category = category_cfg or {}
    base = defaults or {}

    def pick(key: str) -> Any:
        if key in category and category[key] is not None:
            return category[key]
        if key in base and base[key] is not None:
            return base[key]
        return None

    topic_flag = pick("topic_hashtags")
    keep_flag = pick("keep_static_hashtags")
    max_total_flag = pick("max_hashtags")
    max_topic_flag = pick("max_topic_hashtags")
    use_selector = any(
        flag is not None
        for flag in (topic_flag, keep_flag, max_total_flag, max_topic_flag)
    )
    topic_hashtags = _as_bool(topic_flag, False)
    if use_selector:
        keep_static = max(0, min(1, _as_int(keep_flag, 1 if topic_hashtags else 0)))
        max_topic = max(0, min(2, _as_int(max_topic_flag, 2)))
        max_total = max(0, min(3, _as_int(max_total_flag, 3)))
    else:
        keep_static = 0
        max_topic = 0
        max_total = 0
    return EngagementSettings(
        topic_hashtags=topic_hashtags,
        use_selector=use_selector,
        keep_static=keep_static,
        max_topic=max_topic,
        max_total=max_total,
        mentions=_as_bool(pick("mentions"), False),
        max_mentions=max(0, min(2, _as_int(pick("max_mentions"), 2))),
    )


def _char_budget(settings: EngagementSettings, tag: str) -> int:
    """Leave room for the emoji, mentions, and hashtags inside 280 characters."""
    if not settings.use_selector and not settings.mentions:
        return 260
    slots = settings.max_total if settings.use_selector else 0
    reserve = len(tag) + 1 + slots * 18
    if settings.mentions:
        reserve += max(1, settings.max_mentions) * 16
    return max(140, 280 - reserve)


def run_ingest(
    *,
    use_grok: bool = True,
    feeds_path: Path | None = None,
    use_preferences: bool = True,
    client: Any = None,
) -> IngestResult:
    config = load_feeds_config(feeds_path)
    defaults = config.get("defaults") or {}
    max_drafts = int(defaults.get("max_drafts_per_fetch", 14))
    per_category = int(defaults.get("max_drafts_per_category", 2))
    categories = config.get("categories") or {}

    seen = _load_seen()
    result = IngestResult()
    created = 0
    mention_client = client
    mention_client_failed = False

    def client_for_mentions() -> Any:
        nonlocal mention_client, mention_client_failed
        if mention_client is not None:
            return mention_client
        if mention_client_failed:
            return None
        try:
            from x_automation.config import create_client

            mention_client = create_client()
        except Exception as exc:
            mention_client_failed = True
            print(f"  [mentions] Skipping @mentions ({exc})")
            return None
        return mention_client

    names = [str(name) for name in categories.keys()]
    weights: dict[str, float] = {}
    if use_preferences:
        from x_automation.analytics import load_category_weights, order_categories

        weights = load_category_weights()
        names = order_categories(names, weights)

    for category_name in names:
        if created >= max_drafts:
            break
        category_cfg = categories[category_name]
        tag = category_cfg.get("tag", "")
        static_hashtags = [str(tag_name) for tag_name in (category_cfg.get("hashtags") or [])]
        engagement = resolve_engagement(category_cfg, defaults)
        grok_style = category_cfg.get("grok_style", "news_brief")
        voice = category_voice_name(category_cfg)
        feeds = category_cfg.get("feeds") or []
        category_budget = _category_draft_budget(
            per_category,
            max_drafts - created,
            weight=weights.get(category_name, 1.0),
            use_preferences=use_preferences,
        )
        fetch_limit = max(category_budget * 4, 8)

        queues: list[tuple[bool, list[FeedItem]]] = []
        for feed_entry in feeds:
            feed_url = feed_entry.get("url")
            if not feed_url:
                continue
            include_url = bool(feed_entry.get("include_url", True))
            result.feeds_checked += 1
            try:
                items = fetch_feed_items(feed_url, max_items=fetch_limit)
            except Exception as exc:
                result.errors += 1
                result.error_details.append(f"{feed_url}: {exc}")
                continue
            queues.append((include_url, items))

        indexes = [0] * len(queues)
        category_created = 0
        while category_created < category_budget and created < max_drafts:
            progressed = False
            for i, (include_url, items) in enumerate(queues):
                if category_created >= category_budget or created >= max_drafts:
                    break
                while indexes[i] < len(items):
                    item = items[indexes[i]]
                    indexes[i] += 1
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
                            voice=voice,
                            char_budget=_char_budget(engagement, str(tag)),
                        )
                        if is_skipped_post(text):
                            result.skipped_filtered += 1
                            seen.add(key)
                            progressed = True
                            break
                        text, hashtags, handles = _decorate_post(
                            text,
                            item,
                            category=category_name,
                            static_hashtags=static_hashtags,
                            engagement=engagement,
                            use_grok=use_grok,
                            client_for_mentions=client_for_mentions,
                        )
                        if not text.strip():
                            result.skipped_filtered += 1
                            seen.add(key)
                            progressed = True
                            break
                        draft = add_draft(
                            category=category_name,
                            text=text,
                            tag_emoji=tag,
                            hashtags=hashtags,
                            mentions=handles,
                            source="rss+grok" if use_grok else "rss",
                            source_url=item.link,
                            rss_guid=item.guid,
                            feed_url=item.feed_url,
                            metadata={
                                "rss_key": key,
                                "grok_style": grok_style if use_grok else None,
                                "voice": voice if use_grok else None,
                                "title": item.title,
                                "hashtags": hashtags,
                                "mentions": handles,
                            },
                        )
                    except Exception as exc:
                        result.errors += 1
                        result.error_details.append(f"{key}: {exc}")
                        progressed = True
                        break

                    seen.add(key)
                    created += 1
                    category_created += 1
                    result.drafts_created += 1
                    result.created_ids.append(draft["id"])
                    progressed = True
                    break
            if not progressed:
                break

    _save_seen(seen)
    return result


def _category_draft_budget(
    per_category: int,
    remaining: int,
    *,
    weight: float,
    use_preferences: bool,
) -> int:
    """Drafts to attempt for one category, still inside the fetch cap.

    Preferences scale ``max_drafts_per_category`` by engagement weight.
    The result is at least 1 when preferences are on, and at most twice
    the base budget. The fetch cap can still stop the walk early.
    """
    if use_preferences:
        from x_automation.analytics import draft_budget

        intended = draft_budget(per_category, weight)
    else:
        intended = per_category
    if remaining < 1:
        return 0
    return min(intended, remaining)


def _decorate_post(
    text: str,
    item: FeedItem,
    *,
    category: str,
    static_hashtags: list[str],
    engagement: EngagementSettings,
    use_grok: bool,
    client_for_mentions: Any,
) -> tuple[str, list[str], list[str]]:
    """Add story hashtags and verified mentions without failing the draft."""
    notes = Annotations(hashtags=[], mentions=[])
    wants_notes = use_grok and (engagement.topic_hashtags or engagement.mentions)
    if wants_notes:
        try:
            notes = suggest_annotations(
                category=category,
                title=item.title,
                summary=item.summary,
            )
        except Exception:
            notes = Annotations(hashtags=[], mentions=[])

    text = strip_generated_tags(
        text,
        hashtags=engagement.use_selector,
        mentions=engagement.mentions,
    )
    if engagement.use_selector:
        hashtags = select_hashtags(
            notes.hashtags if engagement.topic_hashtags else [],
            static_hashtags,
            keep_static=engagement.keep_static,
            max_topic=engagement.max_topic,
            max_total=engagement.max_total,
        )
    else:
        hashtags = list(static_hashtags)

    handles: list[str] = []
    if engagement.mentions and notes.mentions:
        try:
            handles = verify_mentions(
                notes.mentions,
                client=client_for_mentions(),
                max_mentions=engagement.max_mentions,
            )
        except Exception:
            handles = []
    return text, hashtags, handles


def _item_to_text(
    item: FeedItem,
    *,
    category: str,
    grok_style: str,
    include_url: bool,
    use_grok: bool,
    voice: str | None = None,
    char_budget: int = 260,
) -> str:
    if use_grok:
        return transform_item(
            style=grok_style,
            category=category,
            title=item.title,
            summary=item.summary,
            link=item.link,
            include_url=include_url,
            voice=voice,
            char_budget=char_budget,
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
    print(f"  Skipped (filter):  {result.skipped_filtered}")
    print(f"  Errors:            {result.errors}")
    if result.error_details:
        for detail in result.error_details[:10]:
            print(f"    - {detail}")
