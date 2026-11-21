"""Weighted industry rotation, including the shipped FedRAMP bias."""

from x_automation.linkedin.rotation import choose_industry, weighted_cycle

WEIGHTS = {
    "fedramp": 3,
    "aerospace_defense": 1,
    "tech": 1,
    "aws_cloud": 1,
}
CYCLE = [
    "fedramp",
    "aerospace_defense",
    "fedramp",
    "tech",
    "fedramp",
    "aws_cloud",
]


def test_fedramp_cycle_is_half_and_spread():
    assert weighted_cycle(WEIGHTS) == CYCLE
    assert CYCLE.count("fedramp") == 3
    assert CYCLE[0] != CYCLE[-1]


def test_empty_history_starts_with_fedramp():
    available = set(WEIGHTS)
    assert choose_industry(WEIGHTS, [], available) == "fedramp"


def test_rotation_walks_the_cycle():
    available = set(WEIGHTS)
    history: list[str] = []
    picked = []
    for _ in CYCLE:
        choice = choose_industry(WEIGHTS, history, available)
        assert choice is not None
        picked.append(choice)
        history.append(choice)
    assert picked == CYCLE


def test_missing_fedramp_item_falls_through_without_repeating():
    available = {"aerospace_defense", "tech", "aws_cloud"}
    assert choose_industry(WEIGHTS, [], available) == "aerospace_defense"
    # Previous post was aerospace; do not pick it again while others are open.
    assert choose_industry(WEIGHTS, ["aerospace_defense"], available) == "tech"


def test_only_fedramp_has_news_keeps_posting_fedramp():
    available = {"fedramp"}
    assert choose_industry(WEIGHTS, ["fedramp", "aerospace_defense"], available) == "fedramp"
