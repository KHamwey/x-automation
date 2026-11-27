"""Generate and publish LinkedIn posts with no approval step.

Each run posts at most ``max_posts_per_run`` items (default 1), skips
anything already posted, and stops when the daily cap or minimum spacing
has not elapsed. ``--dry-run`` prints the post and does not call LinkedIn
or write history.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from x_automation.linkedin.client import LinkedInClient, build_client
from x_automation.linkedin.config import (
    DEFAULT_CHAR_BUDGET,
    DEFAULT_DAILY_CAP,
    DEFAULT_DAY_TIMEZONE,
    DEFAULT_MAX_POSTS_PER_RUN,
    DEFAULT_MIN_SPACING_HOURS,
    LINKEDIN_STATE_PATH,
    api_version,
    defaults_of,
    enabled_industries,
    load_linkedin_config,
)
from x_automation.linkedin.errors import LinkedInAuthError
from x_automation.linkedin.feeds import fetch_feed
from x_automation.linkedin.generate import generate_linkedin_post
from x_automation.linkedin.rotation import choose_industry
from x_automation.linkedin.state import (
    count_posts_on_day,
    load_state,
    posted_industries,
    save_state,
    seen_keys,
    seen_links,
    spacing_blocks,
)
from x_automation.linkedin.tokens import expiry_warning, utc_now
from x_automation.rss import FeedItem

GeneratePost = Callable[..., str]
FetchItems = Callable[[str, int], list[FeedItem]]
ImageUploader = Callable[[FeedItem], str | None]

_IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/gif"}
_MAX_IMAGE_BYTES = 8_000_000


@dataclass
class AutopostResult:
    posted: int = 0
    dry_run: int = 0
    skipped_cap: int = 0
    skipped_spacing: int = 0
    skipped_duplicate: int = 0
    feeds_checked: int = 0
    feed_errors: int = 0
    errors: int = 0
    nothing_new: bool = False
    error_details: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    previews: list[str] = field(default_factory=list)


def run_autopost(
    *,
    dry_run: bool = False,
    industry: str | None = None,
    feeds_path: Path | None = None,
    prompts_path: Path | None = None,
    state_path: Path | None = None,
    client: LinkedInClient | None = None,
    now: datetime | None = None,
    fetch_items: FetchItems | None = None,
    generate_post: GeneratePost | None = None,
    image_uploader: ImageUploader | None = None,
) -> AutopostResult:
    config = load_linkedin_config(feeds_path)
    settings = defaults_of(config)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    history_path = state_path or LINKEDIN_STATE_PATH
    state = load_state(history_path)
    cap = int(settings.get("daily_cap", DEFAULT_DAILY_CAP))
    spacing_hours = float(settings.get("min_spacing_hours", DEFAULT_MIN_SPACING_HOURS))
    per_run = int(settings.get("max_posts_per_run", DEFAULT_MAX_POSTS_PER_RUN))
    char_budget = int(settings.get("char_budget", DEFAULT_CHAR_BUDGET))
    day_timezone = str(settings.get("day_timezone") or DEFAULT_DAY_TIMEZONE)
    max_items = int(settings.get("max_items_per_feed", 8))
    attach_images = bool(settings.get("attach_images", False))
    result = AutopostResult()

    today_count = count_posts_on_day(state, moment, day_timezone)
    if today_count >= cap:
        result.skipped_cap = 1
        result.messages.append(
            f"Daily cap reached ({today_count}/{cap}) for {day_timezone}. Nothing published."
        )
        return result
    if spacing_blocks(state, moment, spacing_hours):
        result.skipped_spacing = 1
        result.messages.append(
            f"Last LinkedIn post was under {spacing_hours:g} hours ago. Nothing published."
        )
        return result

    industries = enabled_industries(config)
    if industry:
        industries = [item for item in industries if item["id"] == industry]
        if not industries:
            raise ValueError(
                f"Unknown or disabled industry '{industry}'. "
                "See the ids in linkedin_feeds.yaml."
            )

    owns_client = False
    api: LinkedInClient | None = client
    http: httpx.Client | None = None
    fetcher = fetch_items
    try:
        if not dry_run:
            if api is None:
                api = build_client(api_version=api_version(config))
                owns_client = True
            assert api is not None
            api.ensure_fresh()
            profile = api.get_userinfo()
            warning = expiry_warning(api.token, utc_now())
            if warning:
                result.messages.append(warning)
                print(warning)
            print(
                f"Publishing as {profile.get('name') or api.token.get('member_name') or 'member'} "
                f"(urn:li:person:{profile.get('sub')})"
            )

        if fetcher is None:
            http = httpx.Client(timeout=20.0, follow_redirects=True)

            def fetcher(url: str, limit: int) -> list[FeedItem]:
                assert http is not None
                return fetch_feed(url, max_items=limit, http=http)

        assert fetcher is not None
        writer = generate_post or generate_linkedin_post
        return _publish_from_feeds(
            result,
            industries=industries,
            state=state,
            history_path=history_path,
            moment=moment,
            dry_run=dry_run,
            per_run=per_run,
            cap=cap,
            spacing_hours=spacing_hours,
            day_timezone=day_timezone,
            char_budget=char_budget,
            max_items=max_items,
            attach_images=attach_images,
            prompts_path=prompts_path,
            fetch_items=fetcher,
            writer=writer,
            api=api,
            http=http,
            image_uploader=image_uploader,
        )
    finally:
        if http is not None:
            http.close()
        if owns_client and api is not None:
            api.close()


def _publish_from_feeds(
    result: AutopostResult,
    *,
    industries: list[dict[str, Any]],
    state: dict[str, Any],
    history_path: Path,
    moment: datetime,
    dry_run: bool,
    per_run: int,
    cap: int,
    spacing_hours: float,
    day_timezone: str,
    char_budget: int,
    max_items: int,
    attach_images: bool,
    prompts_path: Path | None,
    fetch_items: FetchItems,
    writer: GeneratePost,
    api: LinkedInClient | None,
    http: httpx.Client | None,
    image_uploader: ImageUploader | None,
) -> AutopostResult:
    known_keys = seen_keys(state)
    known_links = seen_links(state)
    by_id = {item["id"]: item for item in industries}
    candidates: dict[str, list[tuple[str, FeedItem, bool]]] = {item["id"]: [] for item in industries}

    for industry_cfg in industries:
        for feed in industry_cfg["feeds"]:
            url = feed["url"]
            result.feeds_checked += 1
            try:
                items = fetch_items(url, max_items)
            except Exception as exc:
                result.feed_errors += 1
                result.error_details.append(f"{url}: {exc}")
                continue
            for item in items:
                if not item.title:
                    continue
                key = f"{item.feed_url}::{item.guid}"
                if key in known_keys or (item.link and item.link in known_links):
                    result.skipped_duplicate += 1
                    continue
                candidates[industry_cfg["id"]].append(
                    (key, item, bool(feed.get("include_url", True)))
                )

    available = {industry_id for industry_id, rows in candidates.items() if rows}
    if not available:
        if result.feeds_checked and result.feed_errors == result.feeds_checked:
            result.errors = result.feed_errors
            result.messages.append("Every LinkedIn source feed failed.")
        else:
            result.nothing_new = True
            result.messages.append("No unseen industry items to post.")
        return result

    weights = {item["id"]: int(item["weight"]) for item in industries}
    history = posted_industries(state)
    published = 0
    # A second post in the same run would land immediately, so honor minimum
    # spacing after the first real publish. Dry runs do not write history.
    while (
        published < per_run
        and count_posts_on_day(state, moment, day_timezone) < cap
        and not (published and spacing_blocks(state, moment, spacing_hours))
    ):
        chosen = choose_industry(weights, history, available)
        if not chosen:
            break
        queue = candidates[chosen]
        if not queue:
            available.discard(chosen)
            continue
        key, item, include_url = queue.pop(0)
        if not queue:
            available.discard(chosen)
        industry_cfg = by_id[chosen]
        print(f"Selected [{chosen}] {item.title}")
        try:
            text = writer(
                industry_name=industry_cfg["name"],
                angle=industry_cfg["angle"],
                style=industry_cfg["grok_style"],
                title=item.title,
                summary=item.summary,
                link=item.link,
                include_url=include_url and bool(item.link),
                hashtags=industry_cfg["hashtags"],
                char_budget=char_budget,
                prompts_path=prompts_path,
            )
        except Exception as exc:
            result.errors += 1
            result.error_details.append(f"{key}: {exc}")
            return result

        if dry_run:
            result.dry_run += 1
            result.previews.append(text)
            result.messages.append(f"Dry run would publish [{chosen}] {item.title}")
            published += 1
            history.append(chosen)
            continue

        assert api is not None
        image_urn = _maybe_image(
            api,
            item,
            attach=attach_images,
            http=http,
            image_uploader=image_uploader,
            result=result,
        )
        try:
            post_urn = api.create_post(
                text,
                image_urn=image_urn,
                alt_text=item.title[:120] if image_urn else None,
            )
        except LinkedInAuthError:
            raise
        except Exception as exc:
            result.errors += 1
            result.error_details.append(f"{key}: {exc}")
            return result

        entry = {
            "key": key,
            "link": item.link,
            "industry": chosen,
            "title": item.title,
            "excerpt": text[:240],
            "post_urn": post_urn,
            "posted_at": moment.replace(microsecond=0).isoformat(),
        }
        if image_urn:
            entry["image_urn"] = image_urn
        state["posts"].append(entry)
        save_state(state, history_path)
        known_keys.add(key)
        if item.link:
            known_links.add(item.link)
        history.append(chosen)
        result.posted += 1
        result.previews.append(text)
        result.messages.append(f"Published [{chosen}] {post_urn}")
        published += 1
        print(f"  Published {post_urn}")
    return result


def _maybe_image(
    api: LinkedInClient,
    item: FeedItem,
    *,
    attach: bool,
    http: httpx.Client | None,
    image_uploader: ImageUploader | None,
    result: AutopostResult,
) -> str | None:
    if not attach or not item.image_url:
        return None
    try:
        if image_uploader is not None:
            return image_uploader(item)
        if http is None:
            return None
        response = http.get(item.image_url, timeout=20.0, follow_redirects=True)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "image/jpeg").split(";")[0].strip().lower()
        if content_type not in _IMAGE_TYPES:
            result.messages.append(f"Image skipped (content type {content_type or 'unknown'}).")
            return None
        if len(response.content) > _MAX_IMAGE_BYTES:
            result.messages.append("Image skipped (larger than 8MB).")
            return None
        return api.upload_image(
            response.content,
            content_type="image/jpeg" if content_type == "image/jpg" else content_type,
        )
    except Exception as exc:
        result.messages.append(f"Image skipped: {exc}")
        print(f"  [warn] Image upload skipped: {exc}")
        return None


def print_autopost_summary(result: AutopostResult, *, dry_run: bool) -> None:
    mode = "DRY-RUN" if dry_run else "PUBLISH"
    print(f"\n--- LinkedIn autopost ({mode}) ---")
    print(f"  Published:      {result.posted}")
    print(f"  Dry-run posts:  {result.dry_run}")
    print(f"  Skipped cap:    {result.skipped_cap}")
    print(f"  Skipped spacing:{result.skipped_spacing}")
    print(f"  Duplicates:     {result.skipped_duplicate}")
    print(f"  Feeds checked:  {result.feeds_checked}")
    print(f"  Feed errors:    {result.feed_errors}")
    print(f"  Errors:         {result.errors}")
    for message in result.messages:
        print(f"  {message}")
    for preview in result.previews:
        print("\n----- post -----")
        print(preview)
        print("----- end -----")
    for detail in result.error_details[:10]:
        print(f"    - {detail}")
