"""Autopost selection, dedupe, caps, and direct publish. HTTP is mocked."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from x_automation.linkedin.autopost import run_autopost
from x_automation.linkedin.generate import generate_linkedin_post
from x_automation.rss import FeedItem


NOW = datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc)


def _item(feed: str, guid: str, title: str, *, image: str | None = None) -> FeedItem:
    return FeedItem(
        title=title,
        link=f"https://example.com/{guid}",
        summary=f"Summary for {title}",
        guid=guid,
        feed_url=feed,
        image_url=image,
    )


def _write_config(path, *, cap=2, spacing=3, attach_images=False, extra=""):
    path.write_text(
        f"""
defaults:
  daily_cap: {cap}
  min_spacing_hours: {spacing}
  max_posts_per_run: 1
  char_budget: 500
  day_timezone: America/New_York
  attach_images: {str(attach_images).lower()}
industries:
  fedramp:
    enabled: true
    weight: 3
    name: FedRAMP
    grok_style: fedramp_brief
    hashtags: ["#FedRAMP", "#GovCloud"]
    angle: GovCloud, AWS, and defense IT
    feeds:
      - url: https://example.com/fedramp.xml
        include_url: true
  aerospace_defense:
    enabled: true
    weight: 1
    name: Aerospace
    grok_style: aerospace_brief
    hashtags: ["#Aerospace"]
    angle: space and defense
    feeds:
      - url: https://example.com/aero.xml
  disabled_topic:
    enabled: false
    weight: 9
    name: Disabled
    grok_style: tech_brief
    hashtags: ["#Nope"]
    angle: unused
    feeds:
      - url: https://example.com/disabled.xml
{extra}
""",
        encoding="utf-8",
    )


class FakeClient:
    def __init__(self):
        self.token = {
            "access_token": "token",
            "member_id": "abc",
            "expires_at": "2099-01-01T00:00:00+00:00",
        }
        self.posts = []

    def ensure_fresh(self):
        return None

    def get_userinfo(self):
        return {"sub": "abc", "name": "Test Member"}

    def create_post(self, commentary, image_urn=None, alt_text=None):
        self.posts.append(
            {"commentary": commentary, "image_urn": image_urn, "alt_text": alt_text}
        )
        return "urn:li:share:99"


def _generate(**kwargs):
    return (
        f"{kwargs['title']}\n\n"
        "Short paragraph about the source.\n\n"
        "What do you think?"
    )


def test_publish_rotates_and_dedupes(tmp_path):
    feeds = tmp_path / "feeds.yaml"
    state = tmp_path / "state.json"
    _write_config(feeds)
    catalog = {
        "https://example.com/fedramp.xml": [
            _item("https://example.com/fedramp.xml", "f1", "FedRAMP 20x"),
            _item("https://example.com/fedramp.xml", "f2", "FedRAMP second"),
        ],
        "https://example.com/aero.xml": [
            _item("https://example.com/aero.xml", "a1", "Launch window"),
        ],
    }

    def fetch(url, _limit):
        if "disabled" in url:
            raise AssertionError("disabled industry was fetched")
        return catalog[url]

    client = FakeClient()
    first = run_autopost(
        feeds_path=feeds,
        state_path=state,
        client=client,
        now=NOW,
        fetch_items=fetch,
        generate_post=_generate,
    )
    assert first.posted == 1
    assert client.posts[0]["commentary"].startswith("FedRAMP 20x")
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["posts"][0]["industry"] == "fedramp"
    assert saved["posts"][0]["post_urn"] == "urn:li:share:99"
    assert "access_token" not in state.read_text(encoding="utf-8")

    second = run_autopost(
        feeds_path=feeds,
        state_path=state,
        client=client,
        now=NOW + timedelta(hours=4),
        fetch_items=fetch,
        generate_post=_generate,
    )
    assert second.posted == 1
    assert client.posts[1]["commentary"].startswith("Launch window")
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert [post["industry"] for post in saved["posts"]] == ["fedramp", "aerospace_defense"]

    third = run_autopost(
        feeds_path=feeds,
        state_path=state,
        client=client,
        now=NOW + timedelta(hours=8),
        fetch_items=fetch,
        generate_post=_generate,
    )
    # Same Eastern day and the cap is 2, so the third slot does not publish.
    assert third.posted == 0
    assert third.skipped_cap == 1
    assert len(client.posts) == 2


def test_dry_run_does_not_publish_or_record(tmp_path):
    feeds = tmp_path / "feeds.yaml"
    state = tmp_path / "state.json"
    state.write_text('{"schema_version": 1, "posts": []}\n', encoding="utf-8")
    _write_config(feeds)
    client = FakeClient()

    def fetch(url, _limit):
        if "disabled" in url:
            raise AssertionError(url)
        return [_item(url, "only", "Only story")]

    result = run_autopost(
        dry_run=True,
        feeds_path=feeds,
        state_path=state,
        client=client,
        now=NOW,
        fetch_items=fetch,
        generate_post=_generate,
    )
    assert result.dry_run == 1
    assert result.posted == 0
    assert client.posts == []
    assert json.loads(state.read_text(encoding="utf-8"))["posts"] == []
    assert "Only story" in result.previews[0]


def test_spacing_skips_without_fetching(tmp_path):
    feeds = tmp_path / "feeds.yaml"
    state = tmp_path / "state.json"
    _write_config(feeds, spacing=3)
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "posts": [
                    {
                        "key": "old",
                        "industry": "fedramp",
                        "posted_at": (NOW - timedelta(hours=1)).isoformat(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def fetch(_url, _limit):
        raise AssertionError("should not fetch when spacing blocks")

    result = run_autopost(
        feeds_path=feeds,
        state_path=state,
        client=FakeClient(),
        now=NOW,
        fetch_items=fetch,
        generate_post=_generate,
    )
    assert result.skipped_spacing == 1
    assert result.posted == 0


def test_duplicate_link_is_not_posted_again(tmp_path):
    feeds = tmp_path / "feeds.yaml"
    state = tmp_path / "state.json"
    _write_config(feeds, cap=5)
    catalog = {
        "https://example.com/fedramp.xml": [
            _item("https://example.com/fedramp.xml", "f1", "FedRAMP 20x"),
        ],
        "https://example.com/aero.xml": [],
    }

    def fetch(url, _limit):
        return catalog.get(url, [])

    client = FakeClient()
    assert run_autopost(
        feeds_path=feeds,
        state_path=state,
        client=client,
        now=NOW,
        fetch_items=fetch,
        generate_post=_generate,
    ).posted == 1
    again = run_autopost(
        feeds_path=feeds,
        state_path=state,
        client=client,
        now=NOW + timedelta(hours=4),
        fetch_items=fetch,
        generate_post=_generate,
    )
    assert again.posted == 0
    assert again.nothing_new is True
    assert again.skipped_duplicate == 1
    assert len(client.posts) == 1


def test_all_feed_failures_are_errors(tmp_path):
    feeds = tmp_path / "feeds.yaml"
    _write_config(feeds)

    def fetch(_url, _limit):
        raise RuntimeError("feed down")

    result = run_autopost(
        dry_run=True,
        feeds_path=feeds,
        state_path=tmp_path / "state.json",
        now=NOW,
        fetch_items=fetch,
        generate_post=_generate,
    )
    assert result.errors >= 1
    assert result.posted == 0


def test_image_failure_falls_back_to_text(tmp_path):
    feeds = tmp_path / "feeds.yaml"
    _write_config(feeds, attach_images=True)
    client = FakeClient()

    def fetch(url, _limit):
        if "fedramp" not in url:
            return []
        return [
            _item(
                url,
                "f1",
                "FedRAMP image",
                image="https://cdn.example/a.jpg",
            )
        ]

    def uploader(_item):
        raise RuntimeError("upload failed")

    result = run_autopost(
        feeds_path=feeds,
        state_path=tmp_path / "state.json",
        client=client,
        now=NOW,
        fetch_items=fetch,
        generate_post=_generate,
        image_uploader=uploader,
    )
    assert result.posted == 1
    assert client.posts[0]["image_urn"] is None
    assert any("Image skipped" in message for message in result.messages)


def test_image_urn_is_attached_when_upload_works(tmp_path):
    feeds = tmp_path / "feeds.yaml"
    _write_config(feeds, attach_images=True)
    client = FakeClient()

    def fetch(url, _limit):
        if "fedramp" not in url:
            return []
        return [_item(url, "f1", "FedRAMP image", image="https://cdn.example/a.jpg")]

    result = run_autopost(
        feeds_path=feeds,
        state_path=tmp_path / "state.json",
        client=client,
        now=NOW,
        fetch_items=fetch,
        generate_post=_generate,
        image_uploader=lambda _item: "urn:li:image:xyz",
    )
    assert result.posted == 1
    assert client.posts[0]["image_urn"] == "urn:li:image:xyz"


def test_generate_post_calls_grok_and_shapes_the_text(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    monkeypatch.setenv("XAI_MODEL", "grok-3-mini")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.x.ai/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["model"] == "grok-3-mini"
        blob = json.dumps(body)
        assert "FedRAMP" in blob
        assert "Never invent" in blob or "never invent" in blob.lower()
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Agencies reused the authorization."}}]},
        )

    post = generate_linkedin_post(
        industry_name="FedRAMP, GovCloud, and federal cloud",
        angle="Connect AWS GovCloud and defense IT when the source supports it.",
        style="fedramp_brief",
        title="FedRAMP 20x",
        summary="<p>A new authorization boundary.</p>",
        link="https://example.com/story",
        include_url=True,
        hashtags=["#FedRAMP", "#GovCloud"],
        char_budget=1600,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert "Agencies reused the authorization." in post
    assert "https://example.com/story" in post
    assert "?" in post
    assert "#FedRAMP" in post


def test_eastern_day_rolls_at_midnight():
    from x_automation.linkedin.state import count_posts_on_day

    # 03:30 UTC on 21 Nov is still 20 Nov in Eastern (EST, UTC-5).
    now = datetime(2026, 11, 21, 3, 30, tzinfo=timezone.utc)
    state = {
        "posts": [
            {"posted_at": "2026-11-21T02:00:00+00:00", "industry": "fedramp"},
            {"posted_at": "2026-11-20T03:00:00+00:00", "industry": "tech"},
        ]
    }
    assert count_posts_on_day(state, now, "America/New_York") == 1


def test_shipped_config_covers_owner_industries():
    from x_automation.linkedin.config import (
        enabled_industries,
        load_linkedin_config,
        load_linkedin_prompts,
    )
    from x_automation.linkedin.rotation import weighted_cycle

    root_feeds = load_linkedin_config()
    industries = {item["id"]: item for item in enabled_industries(root_feeds)}
    assert set(industries) == {"fedramp", "aerospace_defense", "tech", "aws_cloud"}
    assert industries["fedramp"]["weight"] == 3
    assert industries["aerospace_defense"]["weight"] == 1
    assert industries["tech"]["weight"] == 1
    assert industries["aws_cloud"]["weight"] == 1
    cycle = weighted_cycle({item["id"]: item["weight"] for item in industries.values()})
    # dict order follows the file: fedramp, aerospace, tech, aws.
    assert cycle.count("fedramp") == 3

    fedramp_urls = {feed["url"] for feed in industries["fedramp"]["feeds"]}
    assert "https://federalnewsnetwork.com/tag/fedramp/feed/" in fedramp_urls
    assert "https://fedscoop.com/tag/fedramp/feed/" in fedramp_urls
    assert "https://aws.amazon.com/blogs/publicsector/feed/" in fedramp_urls
    aero_urls = {feed["url"] for feed in industries["aerospace_defense"]["feeds"]}
    assert "https://www.space.com/feeds/all" in aero_urls
    assert "https://www.nasa.gov/feed/" in aero_urls
    assert "https://www.defenseone.com/rss/all/" in aero_urls
    tech_urls = {feed["url"] for feed in industries["tech"]["feeds"]}
    assert "https://feeds.arstechnica.com/arstechnica/index" in tech_urls
    aws_urls = {feed["url"] for feed in industries["aws_cloud"]["feeds"]}
    assert "https://aws.amazon.com/blogs/aws/feed/" in aws_urls
    assert "https://thenewstack.io/feed/" in aws_urls

    prompts = load_linkedin_prompts()
    styles = prompts["styles"]
    for industry in industries.values():
        assert industry["grok_style"] in styles
    fedramp_system = styles["fedramp_brief"]["system"].lower()
    assert "never invent" in fedramp_system
    assert "govcloud" in fedramp_system
    assert "defense" in fedramp_system
    assert "fedramp" in styles["aws_cloud_brief"]["system"].lower()
    assert "fedramp" in styles["aerospace_brief"]["system"].lower()


def test_example_files_match_defaults():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert (root / "linkedin_feeds.yaml").read_text(encoding="utf-8") == (
        root / "linkedin_feeds.yaml.example"
    ).read_text(encoding="utf-8")
    assert (root / "linkedin_prompts.yaml").read_text(encoding="utf-8") == (
        root / "linkedin_prompts.yaml.example"
    ).read_text(encoding="utf-8")


def test_workflow_schedule_and_secrets():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / ".github/workflows/linkedin-autopost.yml").read_text(
        encoding="utf-8"
    )
    assert 'cron: "15 13 * * 1-5"' in text
    assert 'cron: "30 17 * * 1-5"' in text
    assert "secrets.LINKEDIN_TOKEN_JSON" in text
    assert "secrets.XAI_API_KEY" in text
    assert "secrets.LINKEDIN_CLIENT_ID" in text
    assert "secrets.LINKEDIN_CLIENT_SECRET" in text
    assert "state/linkedin_posted.json" in text
    assert "linkedin autopost" in text
