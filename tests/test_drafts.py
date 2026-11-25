"""Unit tests for draft helpers."""

from __future__ import annotations

import unittest

from x_automation.drafts import append_hashtags


class AppendHashtagsTests(unittest.TestCase):
    def test_appends_short_hashtags(self) -> None:
        self.assertEqual(
            append_hashtags("Hello world", ["news", "tech"]),
            "Hello world #news #tech",
        )

    def test_preserves_body_when_hashtags_exceed_limit(self) -> None:
        body = "Breaking: markets rally on rate cut hopes"
        out = append_hashtags(body, ["#" + ("x" * 300)], limit=280)
        self.assertEqual(out, body)
        self.assertTrue(out.startswith("Breaking"))

    def test_truncates_body_to_fit_hashtags(self) -> None:
        body = "A" * 270
        out = append_hashtags(body, ["news", "tech"], limit=280)
        self.assertTrue(out.endswith("#news #tech"))
        self.assertLessEqual(len(out), 280)
        self.assertIn("…", out)

    def test_keeps_fitting_tags_and_drops_oversized_rest(self) -> None:
        out = append_hashtags("Hi", ["ok", "#" + ("y" * 300)], limit=280)
        self.assertEqual(out, "Hi #ok")

    def test_skips_duplicate_hashtags(self) -> None:
        self.assertEqual(
            append_hashtags("Hello #news", ["news", "tech"]),
            "Hello #news #tech",
        )

    def test_no_hashtags_returns_body(self) -> None:
        self.assertEqual(append_hashtags("Hello", None), "Hello")


if __name__ == "__main__":
    unittest.main()
