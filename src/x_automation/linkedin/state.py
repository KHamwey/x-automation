"""Posted-history for LinkedIn autopost.

``state/linkedin_posted.json`` is committed by GitHub Actions so the next
run remembers what already went out. It stores titles, links, and post URNs,
never tokens.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from x_automation.linkedin.tokens import parse_datetime


def empty_state() -> dict[str, Any]:
    return {"schema_version": 1, "posts": []}


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_state()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return empty_state()
    posts = data.get("posts")
    if not isinstance(posts, list):
        posts = []
    return {"schema_version": data.get("schema_version", 1), "posts": posts}


def save_state(state: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": state.get("schema_version", 1),
        "posts": state.get("posts") or [],
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def seen_keys(state: dict[str, Any]) -> set[str]:
    return {str(post["key"]) for post in state.get("posts") or [] if post.get("key")}


def seen_links(state: dict[str, Any]) -> set[str]:
    return {str(post["link"]) for post in state.get("posts") or [] if post.get("link")}


def posted_industries(state: dict[str, Any]) -> list[str]:
    return [str(post["industry"]) for post in state.get("posts") or [] if post.get("industry")]


def local_day(moment: datetime, tz_name: str) -> str:
    timezone = ZoneInfo(tz_name)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo("UTC"))
    return moment.astimezone(timezone).date().isoformat()


def count_posts_on_day(state: dict[str, Any], now: datetime, tz_name: str) -> int:
    today = local_day(now, tz_name)
    count = 0
    for post in state.get("posts") or []:
        raw = post.get("posted_at")
        if not raw:
            continue
        if local_day(parse_datetime(str(raw)), tz_name) == today:
            count += 1
    return count


def latest_posted_at(state: dict[str, Any]) -> datetime | None:
    latest: datetime | None = None
    for post in state.get("posts") or []:
        raw = post.get("posted_at")
        if not raw:
            continue
        moment = parse_datetime(str(raw))
        if latest is None or moment > latest:
            latest = moment
    return latest


def spacing_blocks(state: dict[str, Any], now: datetime, min_hours: float) -> bool:
    latest = latest_posted_at(state)
    if latest is None or min_hours <= 0:
        return False
    return now - latest < timedelta(hours=min_hours)
