"""CLI compatibility and RSS parsing used by LinkedIn autopost."""

import argparse

import feedparser
import pytest

from x_automation.cli import build_parser, cmd_auth, cmd_linkedin_autopost
from x_automation.linkedin.errors import LinkedInAuthError
from x_automation.rss import items_from_parsed


def test_x_commands_stay_dry_run_by_default():
    parser = build_parser()
    publish = parser.parse_args(["drafts", "publish"])
    assert publish.execute is False
    assert publish.func.__name__ == "cmd_drafts_publish"
    delete_all = parser.parse_args(["delete-all"])
    assert delete_all.execute is False
    auth = parser.parse_args(["auth"])
    assert auth.func is cmd_auth


def test_linkedin_commands_parse():
    parser = build_parser()
    autopost = parser.parse_args(
        ["linkedin", "autopost", "--dry-run", "--industry", "fedramp"]
    )
    assert autopost.dry_run is True
    assert autopost.industry == "fedramp"
    assert autopost.func.__name__ == "cmd_linkedin_autopost"
    linkedin_auth = parser.parse_args(["linkedin", "auth"])
    assert linkedin_auth.func.__name__ == "cmd_linkedin_auth"


def test_autopost_auth_failure_exits_2(monkeypatch, capsys):
    def boom(**_kwargs):
        raise LinkedInAuthError("token expired at 2020-01-01")

    monkeypatch.setattr("x_automation.cli.run_autopost", boom)
    code = cmd_linkedin_autopost(argparse.Namespace(dry_run=False, industry=None))
    assert code == 2
    error = capsys.readouterr().err
    assert "LINKEDIN AUTH FAILURE" in error
    assert "gh secret set LINKEDIN_TOKEN_JSON" in error


def test_rss_items_keep_existing_fields_and_optional_image():
    parsed = feedparser.parse(
        b"""<?xml version="1.0"?>
        <rss version="2.0"><channel>
          <item>
            <title>Hello &amp; welcome</title>
            <link>https://example.com/a</link>
            <guid>g1</guid>
            <description>A summary</description>
            <enclosure url="https://cdn.example/a.jpg" type="image/jpeg"/>
          </item>
        </channel></rss>"""
    )
    items = items_from_parsed(parsed, "https://example.com/feed", max_items=5)
    assert len(items) == 1
    assert items[0].title == "Hello & welcome"
    assert items[0].link == "https://example.com/a"
    assert items[0].guid == "g1"
    assert items[0].summary == "A summary"
    assert items[0].feed_url == "https://example.com/feed"
    assert items[0].image_url == "https://cdn.example/a.jpg"


def test_bad_feed_still_raises():
    parsed = feedparser.parse(b"this is not a feed")
    if getattr(parsed, "bozo", False) and not parsed.entries:
        with pytest.raises(RuntimeError):
            items_from_parsed(parsed, "https://example.com/broken", max_items=1)
