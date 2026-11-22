"""Voice and style prompts for Grok drafts. These tests never call xAI or X."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import yaml

from x_automation.grok import (
    build_transform_request,
    category_voice_name,
    resolve_voice_name,
)
from x_automation.ingest import run_ingest
from x_automation.rss import FeedItem

ROOT = Path(__file__).resolve().parents[1]
PROMPTS_EXAMPLE = ROOT / "prompts.yaml.example"
FEEDS_EXAMPLE = ROOT / "feeds.yaml.example"


def _request(
    style: str,
    category: str,
    voice: str | None = None,
    *,
    prompts: dict | None = None,
    title: str = "Council adds a new fee",
    summary: str = "The city council voted to charge residents a new fee.",
) -> object:
    kwargs = {
        "style": style,
        "category": category,
        "title": title,
        "summary": summary,
        "link": "https://example.com/story",
        "char_budget": 260,
        "voice": voice,
    }
    if prompts is None:
        kwargs["prompts_path"] = PROMPTS_EXAMPLE
    else:
        kwargs["prompts"] = prompts
    return build_transform_request(**kwargs)


class VoiceSelectionTests(unittest.TestCase):
    def test_voice_field_beats_tone(self) -> None:
        self.assertEqual(
            category_voice_name(
                {"voice": "sarcastic_sports", "tone": "sarcastic_conservative"}
            ),
            "sarcastic_sports",
        )

    def test_tone_alias_and_blank_voice(self) -> None:
        self.assertEqual(
            category_voice_name({"tone": "sarcastic_sports"}),
            "sarcastic_sports",
        )
        self.assertEqual(
            category_voice_name({"voice": "  ", "tone": "sarcastic_comedy"}),
            "sarcastic_comedy",
        )
        self.assertIsNone(category_voice_name({}))

    def test_example_feeds_set_category_voices(self) -> None:
        feeds = yaml.safe_load(FEEDS_EXAMPLE.read_text(encoding="utf-8"))
        categories = feeds["categories"]
        self.assertEqual(categories["news"]["voice"], "sarcastic_conservative")
        self.assertEqual(categories["news"]["grok_style"], "headline_joke")
        self.assertEqual(categories["comedy"]["voice"], "sarcastic_comedy")
        self.assertEqual(categories["comedy"]["grok_style"], "absurdist")
        self.assertEqual(categories["motocross"]["voice"], "sarcastic_sports")
        self.assertEqual(categories["motocross"]["grok_style"], "race_brief")
        self.assertEqual(categories["news"]["tag"], "🔴")
        self.assertEqual(categories["news"]["hashtags"], ["#News", "#Headlines"])

    def test_omitted_voice_uses_category_defaults(self) -> None:
        prompts = yaml.safe_load(PROMPTS_EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(prompts["default_voice"], "sarcastic_conservative")
        self.assertEqual(resolve_voice_name(prompts, None, "news"), "sarcastic_conservative")
        self.assertEqual(resolve_voice_name(prompts, None, "comedy"), "sarcastic_comedy")
        self.assertEqual(resolve_voice_name(prompts, None, "motocross"), "sarcastic_sports")

    def test_builtin_category_fallback_without_default_voices(self) -> None:
        prompts = yaml.safe_load(PROMPTS_EXAMPLE.read_text(encoding="utf-8"))
        prompts.pop("default_voices", None)
        self.assertEqual(resolve_voice_name(prompts, None, "motocross"), "sarcastic_sports")
        self.assertEqual(resolve_voice_name(prompts, None, "comedy"), "sarcastic_comedy")
        self.assertEqual(resolve_voice_name(prompts, None, "news"), "sarcastic_conservative")

    def test_unknown_voice_raises(self) -> None:
        with self.assertRaises(ValueError) as caught:
            _request("headline_joke", "news", voice="cheerful_neutral")
        self.assertIn("Unknown voice", str(caught.exception))


class PromptCompositionTests(unittest.TestCase):
    def test_news_prompt_is_sarcastic_and_conservative(self) -> None:
        request = _request("headline_joke", "news", voice="sarcastic_conservative")
        system = request.system.lower()
        self.assertEqual(request.voice, "sarcastic_conservative")
        self.assertIn("deadpan", system)
        self.assertIn("sarcastic", system)
        self.assertIn("right-leaning", system)
        self.assertIn("conservative", system)
        self.assertIn("big government", system)
        self.assertIn("bureaucracy", system)
        self.assertIn("legacy-media", system)
        self.assertIn("progressive talking points", system)
        self.assertIn("slurs", system)
        self.assertIn("do not invent", system)
        self.assertIn("skip", system)
        self.assertNotIn("{{voice}}", request.system)
        self.assertNotIn("neutral tone", system)
        self.assertIn("260", request.user)
        self.assertIn("SKIP", request.user)
        self.assertNotIn("https://example.com/story", request.user)
        self.assertEqual(request.temperature, 0.7)

    def test_comedy_slants_only_when_the_source_is_political(self) -> None:
        request = _request("absurdist", "comedy")
        system = request.system.lower()
        self.assertEqual(request.voice, "sarcastic_comedy")
        self.assertIn("deadpan", system)
        self.assertIn("do not go goofy", system)
        self.assertIn("do not force a political point", system)
        self.assertIn("right-leaning", system)
        self.assertIn("legacy-media", system)
        self.assertNotIn("{{voice}}", request.system)

    def test_motocross_stays_on_the_sport(self) -> None:
        request = _request(
            "race_brief",
            "motocross",
            title="Rider takes the moto win",
            summary="The rider went 1-1 aboard a 450.",
        )
        system = request.system.lower()
        self.assertEqual(request.voice, "sarcastic_sports")
        self.assertIn("deadpan", system)
        self.assertIn("sarcasm", system)
        self.assertIn("do not add politics", system)
        self.assertIn("rider", system)
        self.assertNotIn("progressive talking points", system)
        self.assertNotIn("right-leaning", system)
        self.assertNotIn("big government", system)
        self.assertIn("race", request.user.lower())

    def test_voice_is_appended_when_style_omits_placeholder(self) -> None:
        prompts = {
            "default_voice": "sarcastic_conservative",
            "voices": {"sarcastic_conservative": "Voice: skeptical of bureaucracy."},
            "styles": {
                "news_brief": {
                    "system": "Write a short post.",
                    "user": "Headline: {{title}}",
                    "temperature": 0.4,
                    "max_tokens": 80,
                }
            },
        }
        request = _request("news_brief", "news", prompts=prompts)
        self.assertIn("Write a short post.", request.system)
        self.assertIn("skeptical of bureaucracy.", request.system)
        self.assertNotIn("{{voice}}", request.system)

    def test_explicit_voice_overrides_category_default(self) -> None:
        request = _request("headline_joke", "news", voice="sarcastic_sports")
        self.assertEqual(request.voice, "sarcastic_sports")
        self.assertNotIn("progressive talking points", request.system.lower())


class IngestVoiceTests(unittest.TestCase):
    def test_ingest_passes_voice_and_keeps_tags(self) -> None:
        feeds = {
            "defaults": {"max_drafts_per_fetch": 2, "max_drafts_per_category": 1},
            "categories": {
                "news": {
                    "tag": "🔴",
                    "hashtags": ["#News", "#Headlines"],
                    "grok_style": "headline_joke",
                    "voice": "sarcastic_conservative",
                    "feeds": [
                        {"url": "https://example.com/news.xml", "include_url": False}
                    ],
                },
                "motocross": {
                    "tag": "🏁",
                    "hashtags": ["#Motocross", "#MX"],
                    "grok_style": "race_brief",
                    "tone": "sarcastic_sports",
                    "feeds": [{"url": "https://example.com/mx.xml", "include_url": False}],
                },
            },
        }
        news_item = FeedItem(
            title="Council adds a new fee",
            link="https://example.com/story",
            summary="The council voted yes.",
            guid="news-1",
            feed_url="https://example.com/news.xml",
        )
        mx_item = FeedItem(
            title="Rider takes the moto win",
            link="https://example.com/moto",
            summary="Went 1-1.",
            guid="mx-1",
            feed_url="https://example.com/mx.xml",
        )

        def fake_fetch(url: str, max_items: int = 10) -> list[FeedItem]:
            if url.endswith("mx.xml"):
                return [mx_item]
            return [news_item]

        def fake_transform(**kwargs: object) -> str:
            return f"line:{kwargs['voice']}"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feeds_path = root / "feeds.yaml"
            drafts_path = root / "drafts.json"
            seen_path = root / "seen.json"
            feeds_path.write_text(
                yaml.safe_dump(feeds, sort_keys=False),
                encoding="utf-8",
            )

            with ExitStack() as stack:
                stack.enter_context(
                    patch("x_automation.ingest.fetch_feed_items", side_effect=fake_fetch)
                )
                transform = stack.enter_context(
                    patch("x_automation.ingest.transform_item", side_effect=fake_transform)
                )
                stack.enter_context(
                    patch(
                        "x_automation.grok.httpx.Client",
                        side_effect=AssertionError("no API"),
                    )
                )
                stack.enter_context(patch("x_automation.ingest.SEEN_RSS_PATH", seen_path))
                stack.enter_context(patch("x_automation.drafts.DRAFTS_PATH", drafts_path))
                result = run_ingest(use_grok=True, feeds_path=feeds_path)

            self.assertEqual(result.errors, 0, result.error_details)
            self.assertEqual(result.drafts_created, 2)
            got = {
                call.kwargs["style"]: call.kwargs["voice"]
                for call in transform.call_args_list
            }
            self.assertEqual(
                got,
                {
                    "headline_joke": "sarcastic_conservative",
                    "race_brief": "sarcastic_sports",
                },
            )
            self.assertTrue(all(call.kwargs["include_url"] is False for call in transform.call_args_list))

            drafts = json.loads(drafts_path.read_text(encoding="utf-8"))
            by_category = {draft["category"]: draft for draft in drafts}
            self.assertEqual(
                by_category["news"]["text"],
                "🔴 line:sarcastic_conservative #News #Headlines",
            )
            self.assertEqual(by_category["news"]["metadata"]["voice"], "sarcastic_conservative")
            self.assertEqual(
                by_category["motocross"]["text"],
                "🏁 line:sarcastic_sports #Motocross #MX",
            )
            self.assertEqual(by_category["motocross"]["metadata"]["voice"], "sarcastic_sports")
            self.assertNotIn("https://", by_category["news"]["text"])


if __name__ == "__main__":
    unittest.main()
