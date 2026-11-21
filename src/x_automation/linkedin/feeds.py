"""Fetch LinkedIn source feeds with a timeout.

The X ingest path still calls ``feedparser`` directly. Autopost downloads
the feed first so a hung source cannot stall the GitHub Actions job.
"""

from __future__ import annotations

import feedparser
import httpx

from x_automation.rss import FeedItem, items_from_parsed

USER_AGENT = "x-automation-linkedin/0.1"


def fetch_feed(url: str, *, max_items: int, http: httpx.Client) -> list[FeedItem]:
    response = http.get(
        url,
        timeout=20.0,
        follow_redirects=True,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
    )
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    return items_from_parsed(parsed, url, max_items=max_items)
