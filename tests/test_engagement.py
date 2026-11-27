"""Hashtag selection, length trimming, and mention verification.

These tests never call X or xAI. User lookup and chat completions are faked.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import yaml

from x_automation.drafts import (
    append_hashtags,
    compose_post_text,
    ensure_tag_prefix,
    select_hashtags,
    strip_generated_tags,
)
from x_automation.grok import parse_annotations, suggest_annotations
from x_automation.ingest import resolve_engagement, run_ingest
from x_automation.mentions import MentionCandidate, verify_mentions
from x_automation.rss import FeedItem

ROOT = Path(__file__).resolve().parents[1]
PROMPTS_EXAMPLE = ROOT / "prompts.yaml.example"
FEEDS_EXAMPLE = ROOT / "feeds.yaml.example"


class HashtagSelectionTests(unittest.TestCase):
    def test_topic_tags_plus_one_static_tag(self) -> None:
        chosen = select_hashtags(
            ["FBI", "S.N.L.", "Breaking", "News"],
            ["#News", "#Headlines"],
            keep_static=1,
            max_topic=2,
            max_total=3,
        )
        self.assertEqual(chosen, ["#FBI", "#SNL", "#News"])

    def test_drops_generic_topic_tags_and_keeps_specific_ones(self) -> None:
        chosen = select_hashtags(
            ["#Comedy", "Jokes", "Supercross"],
            ["#Motocross", "#MX"],
            keep_static=1,
        )
        self.assertEqual(chosen, ["#Supercross", "#Motocross"])

    def test_keep_static_zero_omits_category_tags(self) -> None:
        chosen = select_hashtags(
            ["#FBI", "#SNL", "#Extra"],
            ["#News", "#Headlines"],
            keep_static=0,
            max_topic=2,
            max_total=3,
        )
        self.assertEqual(chosen, ["#FBI", "#SNL"])

    def test_clamps_counts_and_dedupes(self) -> None:
        chosen = select_hashtags(
            ["#FBI", "fbi", "#SNL", "#NASA"],
            ["#FBI", "#News", "#Headlines"],
            keep_static=5,
            max_topic=9,
            max_total=9,
        )
        self.assertEqual(chosen, ["#FBI", "#SNL", "#News"])

    def test_total_cap_drops_static_tag_after_topic_tags(self) -> None:
        chosen = select_hashtags(
            ["FBI", "SNL"],
            ["#News"],
            keep_static=1,
            max_topic=2,
            max_total=2,
        )
        self.assertEqual(chosen, ["#FBI", "#SNL"])

    def test_invalid_tokens_are_skipped(self) -> None:
        chosen = select_hashtags(["#", "1", "!!", "A"], ["#News"], keep_static=1)
        self.assertEqual(chosen, ["#News"])


class LengthTrimmingTests(unittest.TestCase):
    def test_compose_without_mentions_matches_append_hashtags(self) -> None:
        text = "Hello world"
        self.assertEqual(
            compose_post_text(text, "", ["news", "tech"]),
            append_hashtags(ensure_tag_prefix(text, ""), ["news", "tech"]),
        )

    def test_mentions_follow_the_body_and_stay_within_limit(self) -> None:
        out = compose_post_text(
            "The bureau found another memo.",
            "🔴",
            ["#FBI", "#News"],
            ["@FBI", "@SNL", "@NASA"],
        )
        self.assertTrue(out.startswith("🔴 "))
        self.assertFalse(out.lstrip().startswith("@"))
        self.assertIn("@FBI", out)
        self.assertIn("@SNL", out)
        self.assertNotIn("@NASA", out)
        self.assertTrue(out.index("@FBI") < out.index("#FBI"))
        self.assertLessEqual(len(out), 280)
        self.assertIn("memo", out)

    def test_link_survives_hashtags_and_mentions(self) -> None:
        out = compose_post_text(
            "Story https://example.com/item",
            "",
            ["#FBI"],
            ["@FBI"],
        )
        self.assertIn("https://example.com/item", out)
        self.assertIn("@FBI", out)
        self.assertIn("#FBI", out)
        self.assertLessEqual(len(out), 280)
        self.assertFalse(out.startswith("@"))

    def test_hashtags_do_not_replace_the_body(self) -> None:
        body = "Breaking: markets rally on rate cut hopes"
        out = compose_post_text(body, "", ["#" + ("x" * 300), "#FBI"], ["@" + ("y" * 30)])
        self.assertTrue(out.startswith("Breaking"))
        self.assertIn("#FBI", out)
        self.assertNotIn("@" + ("y" * 30), out)
        self.assertLessEqual(len(out), 280)

    def test_long_body_is_shortened_not_discarded(self) -> None:
        body = "A" * 270
        out = compose_post_text(body, "", ["#FBI", "#SNL"], ["@FBI"], limit=280)
        self.assertLessEqual(len(out), 280)
        self.assertTrue(out.startswith("A"))
        self.assertGreater(out.count("A"), 20)
        self.assertIn("@FBI", out)

    def test_leading_mention_is_not_how_the_post_opens(self) -> None:
        out = compose_post_text("@FBI found a memo", "", ["#FBI"], ["@FBI"])
        self.assertEqual(out, "found a memo @FBI #FBI")
        self.assertFalse(out.startswith("@"))

    def test_url_fragment_is_not_stripped_as_a_hashtag(self) -> None:
        cleaned = strip_generated_tags(
            "Read https://example.com/story#section today #News @FBI",
            hashtags=True,
            mentions=True,
        )
        self.assertIn("https://example.com/story#section", cleaned)
        self.assertNotIn("#News", cleaned)
        self.assertNotIn("@FBI", cleaned)


class _Users:
    def __init__(self, pages: list[dict] | None = None, error: Exception | None = None) -> None:
        self.pages = pages or []
        self.error = error
        self.queries: list[str] = []
        self.pages_pulled = 0

    def search(self, query: str, max_results: int | None = None, user_fields: list[str] | None = None, **kwargs: object):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        self.pages_pulled += 1
        yield {"data": self.pages}
        self.pages_pulled += 1
        yield {"data": [{"id": "other", "name": "Other", "username": "Other", "verified": True}]}


class _Client:
    def __init__(self, users: _Users) -> None:
        self.users = users
        self.access_token = None
        self.bearer_token = None
        self.oauth2_auth = None
        self.session = type("Session", (), {"headers": {}})()


def _user(name: str, username: str, **extra: object) -> dict:
    user = {
        "id": username,
        "name": name,
        "username": username,
        "verified": True,
        "verified_type": "government",
        "parody": False,
    }
    user.update(extra)
    return user


class MentionVerificationTests(unittest.TestCase):
    def test_verified_display_name_match_is_tagged(self) -> None:
        users = _Users([_user("FBI", "FBI")])
        handles = verify_mentions(
            [{"name": "FBI", "kind": "organization", "handle": "DefinitelyFake"}],
            client=_Client(users),
        )
        self.assertEqual(handles, ["@FBI"])
        self.assertEqual(users.queries, ["FBI"])
        self.assertEqual(users.pages_pulled, 1)

    def test_guessed_handle_is_not_used_when_the_name_does_not_match(self) -> None:
        users = _Users([_user("Someone Else", "DefinitelyFake")])
        handles = verify_mentions(
            [{"name": "FBI", "kind": "organization", "handle": "DefinitelyFake"}],
            client=_Client(users),
        )
        self.assertEqual(handles, [])
        self.assertEqual(users.queries, ["FBI"])

    def test_partial_display_name_is_not_a_match(self) -> None:
        users = _Users([_user("FBI Most Wanted", "FBI")])
        handles = verify_mentions(
            [MentionCandidate("FBI", "organization")],
            client=_Client(users),
        )
        self.assertEqual(handles, [])

    def test_emoji_in_the_display_name_still_matches(self) -> None:
        users = _Users([_user("Jett Lawrence 🏁", "jettlawrence", verified_type="blue")])
        handles = verify_mentions(
            [MentionCandidate("Jett Lawrence", "person")],
            client=_Client(users),
        )
        self.assertEqual(handles, ["@jettlawrence"])

    def test_unverified_account_is_not_tagged(self) -> None:
        users = _Users([_user("FBI", "FBI", verified=False, verified_type="none")])
        handles = verify_mentions(
            [MentionCandidate("FBI", "organization")],
            client=_Client(users),
        )
        self.assertEqual(handles, [])

    def test_verified_type_counts_when_the_boolean_is_missing(self) -> None:
        users = _Users([_user("Saturday Night Live", "nbcsnl", verified=None, verified_type="business")])
        handles = verify_mentions(
            [MentionCandidate("Saturday Night Live", "organization")],
            client=_Client(users),
        )
        self.assertEqual(handles, ["@nbcsnl"])

    def test_two_verified_matches_tag_nobody(self) -> None:
        users = _Users(
            [
                _user("FBI", "FBI", id="1"),
                _user("FBI", "FBIGov", id="2"),
            ]
        )
        handles = verify_mentions(
            [MentionCandidate("FBI", "organization")],
            client=_Client(users),
        )
        self.assertEqual(handles, [])

    def test_verified_account_wins_over_an_unverified_duplicate(self) -> None:
        users = _Users(
            [
                _user("FBI", "FBI_fan", id="fan", verified=False, verified_type="none"),
                _user("FBI", "FBI", id="official"),
            ]
        )
        handles = verify_mentions(
            [MentionCandidate("FBI", "organization")],
            client=_Client(users),
        )
        self.assertEqual(handles, ["@FBI"])

    def test_parody_account_is_not_tagged(self) -> None:
        users = _Users([_user("FBI", "FBI", parody=True)])
        handles = verify_mentions(
            [MentionCandidate("FBI", "organization")],
            client=_Client(users),
        )
        self.assertEqual(handles, [])

    def test_lookup_error_tags_nobody(self) -> None:
        users = _Users(error=RuntimeError("network down"))
        handles = verify_mentions(
            [MentionCandidate("FBI", "organization"), MentionCandidate("SNL", "organization")],
            client=_Client(users),
        )
        self.assertEqual(handles, [])
        self.assertEqual(users.queries, ["FBI", "SNL"])

    def test_missing_client_tags_nobody(self) -> None:
        self.assertEqual(
            verify_mentions([MentionCandidate("FBI", "organization")], client=None),
            [],
        )

    def test_private_victim_and_minor_are_not_looked_up(self) -> None:
        users = _Users([_user("Jane Doe", "janedoe"), _user("FBI", "FBI")])
        handles = verify_mentions(
            [
                MentionCandidate("Jane Doe", "private"),
                MentionCandidate("the victim", "person"),
                MentionCandidate("a 12-year-old", "person"),
                MentionCandidate("Alex Witness", "witness"),
                {"name": "FBI", "kind": "organization"},
            ],
            client=_Client(users),
            max_mentions=2,
        )
        self.assertEqual(users.queries, ["FBI"])
        self.assertEqual(handles, ["@FBI"])

    def test_caps_at_two_mentions(self) -> None:
        users = _Users(
            [
                _user("FBI", "FBI", id="1"),
                _user("NASA", "NASA", id="2"),
                _user("SNL", "SNL", id="3"),
            ]
        )
        # Each lookup returns the same page, so every name has to match one row.
        # Split by making search return only the queried name.
        queried: list[str] = []

        def search(query: str, max_results: int | None = None, user_fields: list[str] | None = None, **kwargs: object):
            queried.append(query)
            yield {"data": [_user(query, query, id=query)]}

        users.search = search
        handles = verify_mentions(
            [
                MentionCandidate("FBI", "organization"),
                MentionCandidate("NASA", "organization"),
                MentionCandidate("SNL", "organization"),
            ],
            client=_Client(users),
            max_mentions=2,
        )
        self.assertEqual(handles, ["@FBI", "@NASA"])
        self.assertEqual(queried, ["FBI", "NASA"])


class AnnotationParsingTests(unittest.TestCase):
    def test_parse_ignores_guessed_handles(self) -> None:
        notes = parse_annotations(
            '```json\n{"hashtags": ["FBI", "News"], '
            '"mentions": [{"name": "FBI", "kind": "organization", "handle": "DefinitelyFake"}]}\n```'
        )
        self.assertEqual(notes.hashtags, ["FBI", "News"])
        self.assertEqual(notes.mentions, [MentionCandidate("FBI", "organization")])

    def test_bad_json_is_empty(self) -> None:
        notes = parse_annotations("not json")
        self.assertEqual(notes.hashtags, [])
        self.assertEqual(notes.mentions, [])

    def test_suggest_annotations_uses_the_example_prompt_and_no_live_api(self) -> None:
        prompts = yaml.safe_load(PROMPTS_EXAMPLE.read_text(encoding="utf-8"))
        captured: dict[str, object] = {}

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "hashtags": ["Supercross"],
                                        "mentions": [{"name": "Jett Lawrence", "kind": "person", "handle": "nope"}],
                                    }
                                )
                            }
                        }
                    ]
                }

        class FakeHttp:
            def __init__(self, *args: object, **kwargs: object) -> None:
                return None

            def __enter__(self) -> "FakeHttp":
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def post(self, url: str, headers: dict | None = None, json: dict | None = None) -> FakeResponse:
                captured["url"] = url
                captured["json"] = json
                return FakeResponse()

        def fake_get_env(name: str, default: str | None = None) -> str | None:
            if name == "XAI_API_KEY":
                return "test-key"
            if name == "XAI_MODEL":
                return "grok-3-mini"
            return default

        with ExitStack() as stack:
            stack.enter_context(patch("x_automation.grok.httpx.Client", FakeHttp))
            stack.enter_context(patch("x_automation.grok.get_env", side_effect=fake_get_env))
            notes = suggest_annotations(
                category="motocross",
                title="Jett Lawrence wins Anaheim",
                summary="Lawrence went 1-1.",
                prompts=prompts,
            )

        self.assertEqual(notes.hashtags, ["Supercross"])
        self.assertEqual(notes.mentions, [MentionCandidate("Jett Lawrence", "person")])
        self.assertEqual(captured["url"], "https://api.x.ai/v1/chat/completions")
        payload = captured["json"]
        assert isinstance(payload, dict)
        system = payload["messages"][0]["content"]
        self.assertIn("Supercross", system)
        self.assertIn("minors", system.lower())
        self.assertNotIn("{{category}}", payload["messages"][1]["content"])
        self.assertIn("Jett Lawrence wins Anaheim", payload["messages"][1]["content"])

    def test_suggest_annotations_falls_back_when_the_call_fails(self) -> None:
        prompts = yaml.safe_load(PROMPTS_EXAMPLE.read_text(encoding="utf-8"))

        class FakeHttp:
            def __init__(self, *args: object, **kwargs: object) -> None:
                return None

            def __enter__(self) -> "FakeHttp":
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def post(self, *args: object, **kwargs: object) -> object:
                raise RuntimeError("offline")

        def fake_get_env(name: str, default: str | None = None) -> str | None:
            if name == "XAI_API_KEY":
                return "test-key"
            return default

        with ExitStack() as stack:
            stack.enter_context(patch("x_automation.grok.httpx.Client", FakeHttp))
            stack.enter_context(patch("x_automation.grok.get_env", side_effect=fake_get_env))
            notes = suggest_annotations(
                category="news",
                title="FBI memo",
                summary="A memo.",
                prompts=prompts,
            )
        self.assertEqual(notes.hashtags, [])
        self.assertEqual(notes.mentions, [])

    def test_missing_annotations_key_skips_the_request(self) -> None:
        with patch("x_automation.grok.httpx.Client", side_effect=AssertionError("no API")):
            notes = suggest_annotations(
                category="news",
                title="FBI memo",
                summary="A memo.",
                prompts={"voices": {}, "styles": {}},
            )
        self.assertEqual(notes.hashtags, [])
        self.assertEqual(notes.mentions, [])


class IngestEngagementTests(unittest.TestCase):
    def test_example_config_keys(self) -> None:
        feeds = yaml.safe_load(FEEDS_EXAMPLE.read_text(encoding="utf-8"))
        prompts = yaml.safe_load(PROMPTS_EXAMPLE.read_text(encoding="utf-8"))
        self.assertIn("system", prompts["annotations"])
        self.assertIn("user", prompts["annotations"])
        for name in ("news", "comedy", "motocross"):
            category = feeds["categories"][name]
            self.assertTrue(category["topic_hashtags"])
            self.assertEqual(category["keep_static_hashtags"], 1)
            self.assertEqual(category["max_topic_hashtags"], 2)
            self.assertEqual(category["max_hashtags"], 3)
            self.assertTrue(category["mentions"])
            self.assertEqual(category["max_mentions"], 2)
        self.assertEqual(feeds["categories"]["news"]["hashtags"], ["#News", "#Headlines"])

    def test_missing_keys_keep_the_static_hashtags(self) -> None:
        settings = resolve_engagement({"hashtags": ["#News", "#Headlines"], "tag": "🔴"})
        self.assertFalse(settings.use_selector)
        self.assertFalse(settings.mentions)

    def _ingest(self, users: _Users, annotation: dict | None, *, fail_annotation: bool = False) -> dict:
        feeds = {
            "defaults": {"max_drafts_per_fetch": 1, "max_drafts_per_category": 1},
            "categories": {
                "news": {
                    "tag": "🔴",
                    "hashtags": ["#News", "#Headlines"],
                    "topic_hashtags": True,
                    "keep_static_hashtags": 1,
                    "max_topic_hashtags": 2,
                    "max_hashtags": 3,
                    "mentions": True,
                    "max_mentions": 2,
                    "grok_style": "headline_joke",
                    "voice": "sarcastic_conservative",
                    "feeds": [{"url": "https://example.com/news.xml", "include_url": False}],
                }
            },
        }
        item = FeedItem(
            title="FBI reviews another memo",
            link="https://example.com/story",
            summary="The bureau issued a memo.",
            guid="news-1",
            feed_url="https://example.com/news.xml",
        )

        class FakeResponse:
            def raise_for_status(self) -> None:
                if fail_annotation:
                    raise RuntimeError("offline")

            def json(self) -> dict:
                return {"choices": [{"message": {"content": json.dumps(annotation or {})}}]}

        class FakeHttp:
            def __init__(self, *args: object, **kwargs: object) -> None:
                return None

            def __enter__(self) -> "FakeHttp":
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def post(self, *args: object, **kwargs: object) -> FakeResponse:
                return FakeResponse()

        def fake_get_env(name: str, default: str | None = None) -> str | None:
            if name == "XAI_API_KEY":
                return "test-key"
            if name == "XAI_MODEL":
                return "grok-3-mini"
            return default

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feeds_path = root / "feeds.yaml"
            drafts_path = root / "drafts.json"
            seen_path = root / "seen.json"
            feeds_path.write_text(yaml.safe_dump(feeds, sort_keys=False), encoding="utf-8")
            with ExitStack() as stack:
                stack.enter_context(
                    patch("x_automation.ingest.fetch_feed_items", return_value=[item])
                )
                stack.enter_context(
                    patch(
                        "x_automation.ingest.transform_item",
                        return_value="@FBI found another memo #Breaking",
                    )
                )
                stack.enter_context(patch("x_automation.grok.httpx.Client", FakeHttp))
                stack.enter_context(patch("x_automation.grok.get_env", side_effect=fake_get_env))
                stack.enter_context(patch("x_automation.grok.PROMPTS_PATH", PROMPTS_EXAMPLE))
                stack.enter_context(patch("x_automation.ingest.SEEN_RSS_PATH", seen_path))
                stack.enter_context(patch("x_automation.drafts.DRAFTS_PATH", drafts_path))
                result = run_ingest(use_grok=True, feeds_path=feeds_path, client=_Client(users))
            self.assertEqual(result.errors, 0, result.error_details)
            self.assertEqual(result.drafts_created, 1)
            drafts = json.loads(drafts_path.read_text(encoding="utf-8"))
            return drafts[0]

    def test_draft_gets_topic_hashtag_and_verified_mention(self) -> None:
        draft = self._ingest(
            _Users([_user("FBI", "FBI")]),
            {
                "hashtags": ["FBI", "News", "Breaking"],
                "mentions": [
                    {"name": "FBI", "kind": "organization", "handle": "DefinitelyFake"},
                    {"name": "a local witness", "kind": "victim"},
                ],
            },
        )
        text = draft["text"]
        self.assertTrue(text.startswith("🔴 "))
        self.assertFalse(text.lstrip().startswith("@"))
        self.assertIn("found another memo", text)
        self.assertIn("#FBI", text)
        self.assertIn("#News", text)
        self.assertNotIn("#Headlines", text)
        self.assertNotIn("#Breaking", text)
        self.assertIn("@FBI", text)
        self.assertNotIn("@DefinitelyFake", text)
        self.assertNotIn("witness", text.lower())
        self.assertLessEqual(len(text), 280)
        self.assertEqual(draft["metadata"]["mentions"], ["@FBI"])

    def test_lookup_failure_still_posts_without_a_mention(self) -> None:
        draft = self._ingest(
            _Users(error=RuntimeError("network down")),
            {
                "hashtags": ["FBI"],
                "mentions": [{"name": "FBI", "kind": "organization", "handle": "FBI"}],
            },
        )
        text = draft["text"]
        self.assertIn("#FBI", text)
        self.assertIn("#News", text)
        self.assertNotIn("@", text)
        self.assertTrue(text.startswith("🔴 "))
        self.assertIn("found another memo", text)


if __name__ == "__main__":
    unittest.main()
