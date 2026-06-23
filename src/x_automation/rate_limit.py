"""Rate limit tracking, 429 backoff, and request pacing.

X API v2 enforces per-user rate limits over rolling 15-minute windows.
Exceeding them returns HTTP 429; this module tracks ``x-rate-limit-*``
response headers and adds conservative pacing so long delete runs complete
without hammering the API.

Delete pacing is the bottleneck: the documented limit is 50 DELETEs per 15 min,
but we target 45 with ~20s between requests to stay safely under the cap and
avoid 429 sleeps that would stall the run even longer.

See: https://docs.x.com/x-api/fundamentals/rate-limits
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

import requests

# Per-user limits (documented ceiling) with safety margins baked into min_interval.
BUCKET_CONFIG = {
    # GET /2/users/:id/tweets — 900/15min documented; light pacing only.
    "timeline": {"limit": 850, "window_sec": 900, "min_interval": 0.1},
    # DELETE /2/tweets/:id — 50/15min documented; ~20s spacing ≈ 45/15min.
    "delete": {"limit": 45, "window_sec": 900, "min_interval": 20.0},
    # GET /2/users/me — called once per run.
    "users_me": {"limit": 70, "window_sec": 900, "min_interval": 0.5},
}


@dataclass
class BucketState:
    remaining: int | None = None
    reset_at: float = 0.0
    limit: int = 0
    request_times: list[float] = field(default_factory=list)
    last_request_at: float = 0.0


class RateLimiter:
    """Tracks API rate limits via response headers and enforces safe pacing."""

    def __init__(self) -> None:
        self.buckets: dict[str, BucketState] = {
            name: BucketState(limit=cfg["limit"])
            for name, cfg in BUCKET_CONFIG.items()
        }

    def update_from_response(self, response: requests.Response, bucket: str) -> None:
        """Sync local state from X's x-rate-limit-* headers after each request."""
        headers = response.headers
        if "x-rate-limit-remaining" not in headers:
            return
        state = self.buckets.setdefault(bucket, BucketState())
        state.remaining = int(headers.get("x-rate-limit-remaining", 0))
        state.limit = int(headers.get("x-rate-limit-limit", state.limit))
        reset = headers.get("x-rate-limit-reset")
        if reset:
            state.reset_at = float(reset)

    def wait_for_bucket(self, bucket: str) -> None:
        cfg = BUCKET_CONFIG.get(bucket, {"min_interval": 1.0, "window_sec": 900, "limit": 100})
        state = self.buckets.setdefault(bucket, BucketState(limit=cfg["limit"]))
        now = time.time()

        # Enforce minimum spacing between requests (critical for delete bucket).
        min_interval = cfg.get("min_interval", 1.0)
        if state.last_request_at:
            elapsed = now - state.last_request_at
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)

        # Sliding-window cap: if we've hit the configured limit, wait for the
        # oldest request to fall outside the 15-minute window.
        window_sec = cfg["window_sec"]
        state.request_times = [t for t in state.request_times if now - t < window_sec]
        if len(state.request_times) >= cfg["limit"]:
            oldest = state.request_times[0]
            sleep_for = window_sec - (now - oldest) + random.uniform(0.5, 2.0)
            if sleep_for > 0:
                print(f"  [rate-limit] Pacing {bucket}: sleeping {sleep_for:.0f}s")
                time.sleep(sleep_for)

        # Proactive backoff when headers show the bucket is nearly exhausted.
        if state.remaining is not None and state.remaining <= 1 and state.reset_at > now:
            sleep_for = state.reset_at - now + random.uniform(1.0, 3.0)
            print(f"  [rate-limit] Bucket {bucket} nearly exhausted; sleeping {sleep_for:.0f}s")
            time.sleep(sleep_for)

    def record_request(self, bucket: str) -> None:
        state = self.buckets.setdefault(bucket, BucketState())
        now = time.time()
        state.last_request_at = now
        state.request_times.append(now)

    def handle_rate_limit_response(self, response: requests.Response) -> bool:
        """Return True if a 429 was handled (caller should retry)."""
        if response.status_code != 429:
            return False
        reset = response.headers.get("x-rate-limit-reset")
        if reset:
            wait = max(float(reset) - time.time(), 60.0)
        else:
            wait = 60.0
        # Jitter avoids thundering herd if multiple clients share the same app.
        wait += random.uniform(1.0, 5.0)
        print(f"  [rate-limit] 429 received; sleeping {wait:.0f}s until reset")
        time.sleep(wait)
        return True

    def request(
        self,
        session: requests.Session,
        method: str,
        url: str,
        bucket: str,
        **kwargs: Any,
    ) -> requests.Response:
        """Make an HTTP request with pacing, 429 handling, and header tracking."""
        while True:
            self.wait_for_bucket(bucket)
            response = session.request(method, url, **kwargs)
            self.record_request(bucket)
            self.update_from_response(response, bucket)
            if self.handle_rate_limit_response(response):
                continue
            return response
