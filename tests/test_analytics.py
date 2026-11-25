"""Scoring and engagement-weight math. No network."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from x_automation.analytics import (  # noqa: E402
    category_weights,
    draft_budget,
    next_metric_fields,
    order_categories,
    score_post,
)


class ScorePostTests(unittest.TestCase):
    def test_weighted_public_counts(self) -> None:
        score = score_post(
            {
                "like_count": 1,
                "retweet_count": 2,
                "reply_count": 3,
                "quote_count": 4,
            }
        )
        self.assertEqual(score, 1 + 4 + 6 + 12)

    def test_impressions_add_one_point_per_hundred(self) -> None:
        score = score_post({"like_count": 1}, impression_count=250)
        self.assertEqual(score, 3.5)

    def test_missing_counts_and_impressions_add_nothing(self) -> None:
        self.assertEqual(score_post({}), 0)
        self.assertEqual(score_post(None), 0)
        self.assertEqual(score_post({"like_count": 2}, impression_count=None), 2)
        self.assertEqual(score_post({"bookmark_count": 40}), 0)


class WeightTests(unittest.TestCase):
    def test_samples_under_three_stay_neutral(self) -> None:
        weights = category_weights(
            {
                "news": [100, 100],
                "comedy": [],
            }
        )
        self.assertEqual(weights["news"], 1.0)
        self.assertEqual(weights["comedy"], 1.0)

    def test_weight_is_category_mean_over_median_mean(self) -> None:
        weights = category_weights(
            {
                "low": [0, 10, 20],
                "high": [30, 30, 30],
            }
        )
        self.assertEqual(weights["low"], 0.5)
        self.assertEqual(weights["high"], 1.5)

    def test_weights_clamp_between_half_and_double(self) -> None:
        weights = category_weights(
            {
                "tiny": [1, 1, 1],
                "mid": [10, 10, 10],
                "huge": [1000, 1000, 1000],
            }
        )
        self.assertEqual(weights["tiny"], 0.5)
        self.assertEqual(weights["mid"], 1.0)
        self.assertEqual(weights["huge"], 2.0)

    def test_small_samples_do_not_move_the_median(self) -> None:
        weights = category_weights(
            {
                "established": [10, 10, 10],
                "other": [30, 30, 30],
                "new": [1000],
            }
        )
        self.assertEqual(weights["established"], 0.5)
        self.assertEqual(weights["other"], 1.5)
        self.assertEqual(weights["new"], 1.0)

    def test_even_number_of_means_uses_the_average_of_the_middle(self) -> None:
        weights = category_weights(
            {
                "a": [10, 10, 10],
                "b": [20, 20, 20],
                "c": [30, 30, 30],
                "d": [40, 40, 40],
            }
        )
        self.assertAlmostEqual(weights["a"], 0.5)
        self.assertAlmostEqual(weights["b"], 0.8)
        self.assertAlmostEqual(weights["c"], 1.2)
        self.assertAlmostEqual(weights["d"], 1.6)

    def test_zero_means_stay_neutral_and_a_spike_takes_the_ceiling(self) -> None:
        weights = category_weights(
            {
                "flat": [0, 0, 0],
                "flat2": [0, 0, 0],
                "spike": [9, 9, 9],
            }
        )
        self.assertEqual(weights["flat"], 1.0)
        self.assertEqual(weights["flat2"], 1.0)
        self.assertEqual(weights["spike"], 2.0)

    def test_one_qualified_category_is_its_own_median(self) -> None:
        self.assertEqual(category_weights({"only": [5, 5, 5]})["only"], 1.0)

    def test_higher_weight_is_ordered_first(self) -> None:
        ordered = order_categories(
            ["news", "comedy", "cycling"],
            {"news": 1.0, "comedy": 2.0, "cycling": 1.0},
        )
        self.assertEqual(ordered, ["comedy", "news", "cycling"])


class DraftBudgetTests(unittest.TestCase):
    def test_neutral_weight_keeps_the_base_budget(self) -> None:
        self.assertEqual(draft_budget(2, 1.0), 2)

    def test_budget_never_drops_to_zero(self) -> None:
        self.assertEqual(draft_budget(2, 0.1), 1)
        self.assertEqual(draft_budget(1, 0.5), 1)

    def test_budget_caps_at_double_the_base(self) -> None:
        self.assertEqual(draft_budget(2, 2.0), 4)
        self.assertEqual(draft_budget(2, 5.0), 4)

    def test_budget_rounds_to_the_nearest_draft(self) -> None:
        self.assertEqual(draft_budget(2, 1.2), 2)
        self.assertEqual(draft_budget(2, 1.4), 3)


class MetricFallbackTests(unittest.TestCase):
    def test_named_private_field_is_dropped(self) -> None:
        reduced = next_metric_fields(
            ["public_metrics", "organic_metrics", "non_public_metrics"],
            400,
            '{"detail": "The `non_public_metrics` field is not available"}',
        )
        self.assertEqual(reduced, ["public_metrics", "organic_metrics"])

    def test_unnamed_rejection_falls_back_to_public_metrics(self) -> None:
        reduced = next_metric_fields(
            ["public_metrics", "organic_metrics", "non_public_metrics"],
            403,
            '{"title": "Forbidden", "detail": "Not authorized"}',
        )
        self.assertEqual(reduced, ["public_metrics"])

    def test_auth_failure_is_not_a_field_fallback(self) -> None:
        self.assertIsNone(
            next_metric_fields(
                ["public_metrics", "organic_metrics", "non_public_metrics"],
                401,
                "Unauthorized",
            )
        )

    def test_public_metrics_failure_is_not_retried_as_a_fallback(self) -> None:
        self.assertIsNone(
            next_metric_fields(
                ["public_metrics"],
                400,
                "The `ids` query parameter value is not valid",
            )
        )


if __name__ == "__main__":
    unittest.main()
