"""Fetch and export posts from the authenticated user's timeline."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable

from xdk import Client

from x_automation.config import apply_auth_headers, ensure_data_dir
from x_automation.filters import filter_posts
from x_automation.rate_limit import RateLimiter


TWEET_FIELDS = ["id", "text", "created_at", "referenced_tweets", "author_id"]


def _response_to_dict(response) -> dict:
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        return response.model_dump()
    return {}


def get_authenticated_user(client: Client, rate_limiter: RateLimiter) -> dict:
    apply_auth_headers(client)
    url = f"{client.base_url}/2/users/me"
    response = rate_limiter.request(client.session, "GET", url, bucket="users_me")
    response.raise_for_status()
    return _response_to_dict(response.json()).get("data") or {}


def fetch_user_posts(
    client: Client,
    user_id: str,
    rate_limiter: RateLimiter,
    *,
    emoji_filter: str | None = None,
    on_page: Callable[[list[dict]], None] | None = None,
) -> list[dict]:
    """Paginate GET /2/users/{id}/tweets and return all authored posts."""
    apply_auth_headers(client)
    url = f"{client.base_url}/2/users/{user_id}/tweets"
    params: dict = {
        "max_results": 100,
        "tweet.fields": ",".join(TWEET_FIELDS),
    }

    all_posts: list[dict] = []
    pagination_token: str | None = None

    while True:
        page_params = dict(params)
        if pagination_token:
            page_params["pagination_token"] = pagination_token

        response = rate_limiter.request(
            client.session, "GET", url, params=page_params, bucket="timeline"
        )
        response.raise_for_status()
        body = response.json()
        page_posts = body.get("data") or []

        if emoji_filter:
            page_posts = filter_posts(page_posts, emoji_filter)

        all_posts.extend(page_posts)
        if on_page:
            on_page(page_posts)

        meta = body.get("meta") or {}
        pagination_token = meta.get("next_token")
        if not pagination_token:
            break

    return all_posts


def save_inventory(posts: list[dict], user: dict) -> str:
    """Write fetched posts to data/inventory-{timestamp}.json; return path."""
    from x_automation.config import DATA_DIR

    ensure_data_dir()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    inventory_path = DATA_DIR / f"inventory-{timestamp}.json"
    payload = {
        "exported_at": timestamp,
        "user": user,
        "count": len(posts),
        "posts": posts,
    }
    with inventory_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return str(inventory_path)


def preview_posts(posts: list[dict], limit: int = 20) -> None:
    print(f"\n{'ID':<22} {'Created':<22} Text preview")
    print("-" * 80)
    for post in posts[:limit]:
        post_id = post.get("id", "")
        created = (post.get("created_at") or "")[:19]
        text = (post.get("text") or "").replace("\n", " ")
        if len(text) > 45:
            text = text[:42] + "..."
        print(f"{post_id:<22} {created:<22} {text}")
    if len(posts) > limit:
        print(f"... and {len(posts) - limit} more")
