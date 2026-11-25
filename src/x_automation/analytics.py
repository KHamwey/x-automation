"""Engagement scores and category draft weights.

Loads ``data/publish_log.json`` and looks up those post IDs only
(``GET /2/tweets?ids=...``, batches of 100). Does not walk the timeline.

Score = likes + 2*reposts + 2*replies + 3*quotes, plus impressions/100
when an impression count is present. Category score is the mean of its
post scores. Categories with fewer than 3 scored posts keep weight 1.0.
Otherwise weight = category mean / median of those means, clamped to
0.5–2.0. Results are written to ``data/engagement.json``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import yaml
from xdk import Client

from x_automation.config import (
    ENGAGEMENT_PATH,
    FEEDS_PATH,
    OWNED_READ_COST,
    PUBLISH_LOG_PATH,
    apply_auth_headers,
    ensure_data_dir,
)
from x_automation.rate_limit import RateLimiter

MIN_POSTS_FOR_WEIGHT = 3
WEIGHT_FLOOR = 0.5
WEIGHT_CEILING = 2.0
LOOKUP_BATCH_SIZE = 100
METRIC_FIELDS = ("public_metrics", "organic_metrics", "non_public_metrics")
PRIVATE_METRIC_FIELDS = ("organic_metrics", "non_public_metrics")
# Prefer owned-tweet metrics for impressions and clicks; public_metrics is the fallback.
IMPRESSION_SOURCES = ("organic_metrics", "non_public_metrics", "public_metrics")


@dataclass
class AnalyticsResult:
    engagement: dict[str, Any]
    owned_reads: int = 0
    posts_requested: int = 0
    estimated_cost: float = 0.0
    fallback_notes: list[str] = field(default_factory=list)
    max_drafts_per_category: int = 2


def score_post(
    public_metrics: dict[str, Any] | None,
    *,
    impression_count: int | None = None,
) -> float:
    """Engagement score for one post. Missing counts count as zero."""
    metrics = public_metrics or {}
    score = (
        _as_int(metrics.get("like_count"))
        + 2 * _as_int(metrics.get("retweet_count"))
        + 2 * _as_int(metrics.get("reply_count"))
        + 3 * _as_int(metrics.get("quote_count"))
    )
    if impression_count is not None:
        score += impression_count / 100
    return float(score)


def mean_score(scores: list[float]) -> float:
    if not scores:
        return 0.0
    return sum(scores) / len(scores)


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2


def category_weights(scores_by_category: dict[str, list[float]]) -> dict[str, float]:
    """Map each category to a draft-budget weight.

    Samples under 3 posts (including empty categories) stay at 1.0 and do
    not affect the median. Qualified weights are category mean / median of
    those means, clamped to 0.5–2.0. A non-positive median cannot be a
    divisor: zero means stay at 1.0 and any positive mean takes the ceiling.
    """
    qualified_means: list[float] = []
    means: dict[str, float] = {}
    for category, scores in scores_by_category.items():
        means[category] = mean_score(scores)
        if len(scores) >= MIN_POSTS_FOR_WEIGHT:
            qualified_means.append(means[category])

    median_mean = median(qualified_means) if qualified_means else 0.0
    weights: dict[str, float] = {}
    for category, scores in scores_by_category.items():
        if len(scores) < MIN_POSTS_FOR_WEIGHT:
            weights[category] = 1.0
            continue
        weights[category] = _weight_from_mean(means[category], median_mean)
    return weights


def draft_budget(max_drafts_per_category: int, weight: float) -> int:
    """Turn a weight into a per-category draft cap. Never returns zero."""
    base = max(int(max_drafts_per_category), 1)
    upper = base * 2
    raw = round(base * weight)
    if raw < 1:
        return 1
    if raw > upper:
        return upper
    return int(raw)


def order_categories(names: list[str], weights: dict[str, float]) -> list[str]:
    """Highest weight first. Equal weights keep their existing order."""
    indexed = list(enumerate(names))
    indexed.sort(key=lambda item: (-weights.get(item[1], 1.0), item[0]))
    return [name for _, name in indexed]


def load_category_weights() -> dict[str, float]:
    """Weights from the last analytics run. Missing file yields an empty map."""
    data = load_engagement()
    if not data:
        return {}
    weights: dict[str, float] = {}
    for name, info in (data.get("categories") or {}).items():
        if not isinstance(info, dict):
            continue
        try:
            weights[str(name)] = float(info.get("weight", 1.0))
        except (TypeError, ValueError):
            weights[str(name)] = 1.0
    return weights


def load_engagement() -> dict[str, Any] | None:
    if not ENGAGEMENT_PATH.exists():
        return None
    try:
        with ENGAGEMENT_PATH.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def top_category_summary() -> dict[str, Any] | None:
    """Highest-mean category, or None when no engagement file or scored posts."""
    data = load_engagement()
    if not data:
        return None
    ranked: list[dict[str, Any]] = []
    for name, info in (data.get("categories") or {}).items():
        if not isinstance(info, dict):
            continue
        try:
            sample = int(info.get("sample_size") or 0)
            if sample < 1 or info.get("mean_score") is None:
                continue
            ranked.append(
                {
                    "category": str(name),
                    "mean_score": float(info["mean_score"]),
                    "weight": float(info.get("weight", 1.0)),
                    "sample_size": sample,
                }
            )
        except (TypeError, ValueError):
            continue
    if not ranked:
        return None
    ranked.sort(key=lambda row: (-row["mean_score"], -row["weight"], row["category"]))
    return ranked[0]


def published_post_ids() -> list[str]:
    _grouped, ordered_ids = _posts_from_log(_load_publish_entries())
    return ordered_ids


def run_analytics(client: Client | None = None) -> AnalyticsResult:
    """Fetch metrics for published posts, score them, and write engagement.json."""
    entries = _load_publish_entries()
    grouped, ordered_ids = _posts_from_log(entries)
    per_category, feed_categories = _feed_preference_context()

    tweets_by_id: dict[str, dict[str, Any]] = {}
    fields_used: list[str] = []
    notes: list[str] = []
    if ordered_ids:
        if client is None:
            raise RuntimeError(
                "No X client available. Run auth before drafts analytics."
            )
        tweets_by_id, fields_used, notes = fetch_post_metrics(client, ordered_ids)
        notes.extend(_missing_metric_notes(tweets_by_id, fields_used))

    engagement = _assemble_engagement(
        grouped=grouped,
        ordered_ids=ordered_ids,
        tweets_by_id=tweets_by_id,
        feed_categories=feed_categories,
        fields_used=fields_used,
        notes=notes,
    )
    _save_engagement(engagement)
    owned_reads = len(tweets_by_id)
    return AnalyticsResult(
        engagement=engagement,
        owned_reads=owned_reads,
        posts_requested=len(ordered_ids),
        estimated_cost=owned_reads * OWNED_READ_COST,
        fallback_notes=list(engagement.get("fallback_notes") or []),
        max_drafts_per_category=per_category,
    )


def fetch_post_metrics(
    client: Client,
    post_ids: list[str],
    rate_limiter: RateLimiter | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    """Look up metrics for explicit post IDs. Returns tweets, fields used, notes."""
    limiter = rate_limiter or RateLimiter()
    tweets_by_id: dict[str, dict[str, Any]] = {}
    notes: list[str] = []
    fields = list(METRIC_FIELDS)
    for batch in _batches(post_ids, LOOKUP_BATCH_SIZE):
        batch_tweets, fields, batch_notes = _fetch_batch(client, limiter, batch, fields)
        notes.extend(batch_notes)
        for tweet in batch_tweets:
            tweet_id = str(tweet.get("id") or "")
            if tweet_id:
                tweets_by_id[tweet_id] = tweet
    missing = len(post_ids) - len(tweets_by_id)
    if missing:
        notes.append(
            f"{missing} of {len(post_ids)} posts were not returned and were left unscored."
        )
    return tweets_by_id, fields, notes


def next_metric_fields(
    requested: list[str],
    status_code: int,
    body: str,
) -> list[str] | None:
    """Reduced tweet.fields after a private-metric rejection, or None to raise.

    A 400/403 while organic or non-public metrics are still requested drops
    the named field, or every private field when the body does not name one.
    The caller retries. Unrelated statuses are not treated as a fallback.
    """
    if status_code not in (400, 403):
        return None
    private_requested = [name for name in PRIVATE_METRIC_FIELDS if name in requested]
    if not private_requested:
        return None
    text = _rejection_text(body)
    named = [name for name in private_requested if name in text]
    drop = named or list(private_requested)
    reduced = [name for name in requested if name not in drop]
    if "public_metrics" not in reduced:
        reduced.insert(0, "public_metrics")
    if reduced == list(requested):
        return None
    return reduced


def impression_from_tweet(tweet: dict[str, Any]) -> tuple[int | None, str | None]:
    """Return (impression_count, source field). Owned metrics win over public."""
    for key in IMPRESSION_SOURCES:
        metrics = tweet.get(key)
        if not isinstance(metrics, dict) or metrics.get("impression_count") is None:
            continue
        try:
            return int(metrics["impression_count"]), key
        except (TypeError, ValueError):
            continue
    return None, None


def print_analytics(result: AnalyticsResult) -> None:
    rows = ranked_rows(result.engagement, result.max_drafts_per_category)
    print("\nEngagement by category")
    print(f"{'Category':<14} {'Posts':>5} {'Mean':>8} {'Weight':>7} {'Budget':>6}")
    print("-" * 44)
    if not rows:
        print("  (no categories)")
    for row in rows:
        mean = f"{row['mean_score']:.2f}" if row["sample_size"] else "—"
        print(
            f"{row['category']:<14} "
            f"{row['sample_size']:>5} "
            f"{mean:>8} "
            f"{row['weight']:>7.2f} "
            f"{row['budget']:>6}"
        )

    fields_used = result.engagement.get("metrics_fields_used") or []
    if fields_used:
        print(f"\nMetrics fields: {', '.join(fields_used)}")
    else:
        print("\nNo published posts to score. Category weights stay at 1.0.")
    for note in result.fallback_notes:
        print(f"  {note}")

    print(
        f"Posts scored: {result.engagement.get('posts_scored', 0)} "
        f"of {result.posts_requested} requested"
    )
    print(
        f"Estimated owned-read cost: ${result.estimated_cost:.3f} "
        f"({result.owned_reads} posts × ${OWNED_READ_COST:.3f})"
    )
    print(f"Wrote {ENGAGEMENT_PATH}")
    print("No posts were published.")


def ranked_rows(
    engagement: dict[str, Any],
    max_drafts_per_category: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, info in (engagement.get("categories") or {}).items():
        if not isinstance(info, dict):
            continue
        sample = int(info.get("sample_size") or 0)
        mean = info.get("mean_score")
        weight = float(info.get("weight", 1.0))
        rows.append(
            {
                "category": str(name),
                "sample_size": sample,
                "mean_score": float(mean) if mean is not None else 0.0,
                "weight": weight,
                "budget": draft_budget(max_drafts_per_category, weight),
            }
        )
    rows.sort(key=lambda row: _category_sort_key(row, row["category"]))
    return rows


def _category_sort_key(info: dict[str, Any], name: str) -> tuple:
    sample = int(info.get("sample_size") or 0)
    mean = info.get("mean_score")
    mean_value = float(mean) if mean is not None else 0.0
    weight = float(info.get("weight", 1.0))
    return (sample == 0, -mean_value, -weight, name)


def _weight_from_mean(category_mean: float, median_mean: float) -> float:
    if median_mean <= 0:
        if category_mean <= 0:
            return 1.0
        return WEIGHT_CEILING
    raw = category_mean / median_mean
    return _clamp(raw, WEIGHT_FLOOR, WEIGHT_CEILING)


def _clamp(value: float, low: float, high: float) -> float:
    if value < low:
        return low
    if value > high:
        return high
    return value


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _load_publish_entries() -> list[dict[str, Any]]:
    if not PUBLISH_LOG_PATH.exists():
        return []
    try:
        with PUBLISH_LOG_PATH.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def _posts_from_log(
    entries: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    ordered_ids: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        post_id = str(entry.get("x_post_id") or "").strip()
        if not post_id or post_id in seen:
            continue
        seen.add(post_id)
        ordered_ids.append(post_id)
        category = str(entry.get("category") or "unknown")
        grouped.setdefault(category, []).append(entry)
    return grouped, ordered_ids


def _feed_preference_context() -> tuple[int, list[str]]:
    if not FEEDS_PATH.exists():
        return 2, []
    try:
        with FEEDS_PATH.open(encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return 2, []
    defaults = config.get("defaults") or {}
    try:
        per_category = int(defaults.get("max_drafts_per_category", 2))
    except (TypeError, ValueError):
        per_category = 2
    names = [str(name) for name in (config.get("categories") or {}).keys()]
    return per_category, names


def _fetch_batch(
    client: Client,
    rate_limiter: RateLimiter,
    post_ids: list[str],
    fields: list[str],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    current = list(fields) or ["public_metrics"]
    notes: list[str] = []
    while True:
        response = _lookup_tweets(client, rate_limiter, post_ids, current)
        if response.ok:
            body = response.json()
            if not isinstance(body, dict):
                raise RuntimeError("Unexpected tweets response from GET /2/tweets.")
            tweets = body.get("data") or []
            return [tweet for tweet in tweets if isinstance(tweet, dict)], current, notes
        reduced = next_metric_fields(current, response.status_code, response.text or "")
        if reduced is None:
            response.raise_for_status()
        dropped = [name for name in current if name not in reduced]
        notes.append(
            f"{', '.join(dropped)} rejected (HTTP {response.status_code}); "
            f"fell back to {', '.join(reduced)}."
        )
        current = reduced


def _lookup_tweets(
    client: Client,
    rate_limiter: RateLimiter,
    post_ids: list[str],
    fields: list[str],
):
    apply_auth_headers(client)
    url = f"{client.base_url}/2/tweets"
    params = {
        "ids": ",".join(post_ids),
        "tweet.fields": ",".join(fields),
    }
    return rate_limiter.request(
        client.session,
        "GET",
        url,
        params=params,
        bucket="lookup",
    )


def _rejection_text(body: str) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body
    parts: list[str] = []
    if isinstance(payload, dict):
        for key in ("detail", "title", "message"):
            value = payload.get(key)
            if isinstance(value, str):
                parts.append(value)
        for error in payload.get("errors") or []:
            if not isinstance(error, dict):
                continue
            for key in ("message", "detail", "title"):
                value = error.get(key)
                if isinstance(value, str):
                    parts.append(value)
    return "\n".join(parts) if parts else body


def _missing_metric_notes(
    tweets_by_id: dict[str, dict[str, Any]],
    fields_used: list[str],
) -> list[str]:
    notes: list[str] = []
    total = len(tweets_by_id)
    if not total:
        return notes
    for name in PRIVATE_METRIC_FIELDS:
        if name not in fields_used:
            continue
        missing = sum(
            1
            for tweet in tweets_by_id.values()
            if not isinstance(tweet.get(name), dict)
        )
        if missing:
            notes.append(
                f"{name} missing on {missing} of {total} posts; "
                "fell back to public_metrics for those posts."
            )
    without_impressions = sum(
        1 for tweet in tweets_by_id.values() if impression_from_tweet(tweet)[0] is None
    )
    if without_impressions:
        notes.append(
            f"impression_count unavailable for {without_impressions} of {total} posts; "
            "those scores omit the impression term."
        )
    return notes


def _click_counts(tweet: dict[str, Any]) -> tuple[int | None, int | None]:
    url_clicks: int | None = None
    profile_clicks: int | None = None
    for key in PRIVATE_METRIC_FIELDS:
        metrics = tweet.get(key)
        if not isinstance(metrics, dict):
            continue
        if url_clicks is None and metrics.get("url_link_clicks") is not None:
            url_clicks = _as_int(metrics.get("url_link_clicks"))
        if profile_clicks is None and metrics.get("user_profile_clicks") is not None:
            profile_clicks = _as_int(metrics.get("user_profile_clicks"))
    return url_clicks, profile_clicks


def _post_record(entry: dict[str, Any], tweet: dict[str, Any]) -> dict[str, Any]:
    public_metrics = tweet.get("public_metrics")
    if not isinstance(public_metrics, dict):
        public_metrics = {}
    impressions, impression_source = impression_from_tweet(tweet)
    url_clicks, profile_clicks = _click_counts(tweet)
    raw_score = score_post(public_metrics, impression_count=impressions)
    return {
        "x_post_id": str(tweet.get("id") or entry.get("x_post_id") or ""),
        "draft_id": entry.get("draft_id"),
        "date_utc": entry.get("date_utc"),
        "score": round(raw_score, 4),
        "like_count": _as_int(public_metrics.get("like_count")),
        "retweet_count": _as_int(public_metrics.get("retweet_count")),
        "reply_count": _as_int(public_metrics.get("reply_count")),
        "quote_count": _as_int(public_metrics.get("quote_count")),
        "impression_count": impressions,
        "impression_source": impression_source,
        "url_link_clicks": url_clicks,
        "user_profile_clicks": profile_clicks,
    }


def _assemble_engagement(
    *,
    grouped: dict[str, list[dict[str, Any]]],
    ordered_ids: list[str],
    tweets_by_id: dict[str, dict[str, Any]],
    feed_categories: list[str],
    fields_used: list[str],
    notes: list[str],
) -> dict[str, Any]:
    posts_by_category: dict[str, list[dict[str, Any]]] = {}
    for category in feed_categories:
        posts_by_category.setdefault(category, [])
    for category, entries in grouped.items():
        bucket = posts_by_category.setdefault(category, [])
        for entry in entries:
            post_id = str(entry.get("x_post_id") or "")
            tweet = tweets_by_id.get(post_id)
            if tweet is None:
                continue
            bucket.append(_post_record(entry, tweet))

    score_lists = {
        category: [float(post["score"]) for post in posts]
        for category, posts in posts_by_category.items()
    }
    weights = category_weights(score_lists)
    qualified = [
        mean_score(scores)
        for scores in score_lists.values()
        if len(scores) >= MIN_POSTS_FOR_WEIGHT
    ]

    categories: dict[str, Any] = {}
    for category, posts in posts_by_category.items():
        sample = len(posts)
        categories[category] = {
            "posts": posts,
            "mean_score": round(mean_score(score_lists[category]), 4) if sample else None,
            "sample_size": sample,
            "weight": round(weights.get(category, 1.0), 4),
        }

    ranked_names = sorted(
        categories,
        key=lambda name: _category_sort_key(categories[name], name),
    )
    ordered_categories = {name: categories[name] for name in ranked_names}

    return {
        "computed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "posts_requested": len(ordered_ids),
        "posts_scored": sum(len(posts) for posts in posts_by_category.values()),
        "metrics_fields_used": list(fields_used),
        "fallback_notes": notes,
        "median_mean": round(median(qualified), 4) if qualified else None,
        "min_posts_for_weight": MIN_POSTS_FOR_WEIGHT,
        "weight_floor": WEIGHT_FLOOR,
        "weight_ceiling": WEIGHT_CEILING,
        "categories": ordered_categories,
    }


def _save_engagement(engagement: dict[str, Any]) -> None:
    ensure_data_dir()
    with ENGAGEMENT_PATH.open("w", encoding="utf-8") as handle:
        json.dump(engagement, handle, indent=2)
        handle.write("\n")


def _batches(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]
