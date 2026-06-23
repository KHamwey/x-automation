"""Publish approved drafts to X with daily per-category caps."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from xdk import Client

from x_automation.config import (
    PUBLISH_LOG_PATH,
    ensure_data_dir,
    apply_auth_headers,
    create_client,
)
from x_automation.ingest import get_daily_publish_cap
from x_automation.drafts import estimate_post_cost, list_drafts, mark_published
from x_automation.rate_limit import RateLimiter


@dataclass
class PublishResult:
    attempted: int = 0
    published: int = 0
    skipped_cap: int = 0
    skipped_status: int = 0
    errors: int = 0
    error_details: list[str] = field(default_factory=list)
    estimated_cost: float = 0.0

    def add_cost(self, amount: float) -> None:
        self.estimated_cost += amount


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_publish_log() -> list[dict[str, Any]]:
    if not PUBLISH_LOG_PATH.exists():
        return []
    with PUBLISH_LOG_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def _save_publish_log(entries: list[dict[str, Any]]) -> None:
    ensure_data_dir()
    with PUBLISH_LOG_PATH.open("w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)


def published_today(category: str) -> int:
    today = _utc_today()
    return sum(
        1
        for entry in _load_publish_log()
        if entry.get("date_utc") == today and entry.get("category") == category
    )


def can_publish(category: str, cap: int | None = None) -> bool:
    limit = cap if cap is not None else get_daily_publish_cap()
    return published_today(category) < limit


def daily_cap() -> int:
    return get_daily_publish_cap()


def record_publish(category: str, draft_id: str, x_post_id: str) -> None:
    entries = _load_publish_log()
    entries.append(
        {
            "date_utc": _utc_today(),
            "category": category,
            "draft_id": draft_id,
            "x_post_id": x_post_id,
            "published_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }
    )
    _save_publish_log(entries)


def stats_today() -> dict[str, int]:
    today = _utc_today()
    counts: dict[str, int] = {}
    for entry in _load_publish_log():
        if entry.get("date_utc") == today:
            cat = entry.get("category", "unknown")
            counts[cat] = counts.get(cat, 0) + 1
    return counts


def _create_post(client: Client, text: str, rate_limiter: RateLimiter) -> str:
    apply_auth_headers(client)
    url = f"{client.base_url}/2/tweets"
    response = rate_limiter.request(
        client.session,
        "POST",
        url,
        json={"text": text},
        bucket="post",
    )
    response.raise_for_status()
    body = response.json()
    data = body.get("data") or {}
    post_id = data.get("id")
    if not post_id:
        raise RuntimeError(f"Unexpected create response: {body}")
    return str(post_id)


def run_publish(
    client: Client | None = None,
    *,
    execute: bool = False,
    draft_id: str | None = None,
    daily_cap_override: int | None = None,
) -> PublishResult:
    cap = daily_cap_override if daily_cap_override is not None else daily_cap()
    result = PublishResult()
    rate_limiter = RateLimiter()

    if draft_id:
        candidates = [d for d in list_drafts() if d.get("id") == draft_id]
    else:
        candidates = list_drafts("approved")

    candidates.sort(key=lambda d: d.get("approved_at") or d.get("created_at") or "")

    if not candidates:
        return result

    api_client = client or create_client()
    category_counts = stats_today()

    for draft in candidates:
        result.attempted += 1
        status = draft.get("status")
        if status != "approved":
            result.skipped_status += 1
            continue

        category = draft.get("category", "unknown")
        if category_counts.get(category, 0) >= cap:
            result.skipped_cap += 1
            print(
                f"  [skip] Daily cap reached for '{category}' "
                f"({category_counts.get(category, 0)}/{cap})"
            )
            continue

        text = draft.get("text", "")
        cost = estimate_post_cost(text)
        result.add_cost(cost)

        if not execute:
            print(
                f"  [dry-run] Would publish {draft.get('id')} "
                f"[{category}] est. ${cost:.3f}: {text[:60]}..."
            )
            category_counts[category] = category_counts.get(category, 0) + 1
            result.published += 1
            continue

        try:
            x_post_id = _create_post(api_client, text, rate_limiter)
            mark_published(draft["id"], x_post_id)
            record_publish(category, draft["id"], x_post_id)
            category_counts[category] = category_counts.get(category, 0) + 1
            result.published += 1
            print(f"  Published {draft['id']} -> {x_post_id} [{category}]")
        except Exception as exc:
            result.errors += 1
            msg = f"{draft.get('id')}: {exc}"
            result.error_details.append(msg)
            print(f"  [ERROR] {msg}")

    return result


def print_publish_summary(result: PublishResult, *, execute: bool) -> None:
    mode = "EXECUTE" if execute else "DRY-RUN"
    print(f"\n--- Publish summary ({mode}) ---")
    print(f"  Attempted:   {result.attempted}")
    print(f"  Published:   {result.published}")
    print(f"  Skipped cap: {result.skipped_cap}")
    print(f"  Skipped bad status: {result.skipped_status}")
    print(f"  Errors:      {result.errors}")
    print(f"  Est. cost:   ${result.estimated_cost:.3f}")
    if result.error_details:
        for detail in result.error_details[:10]:
            print(f"    - {detail}")
