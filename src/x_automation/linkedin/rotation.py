"""Weighted industry rotation.

FedRAMP is configured with a higher weight than the other topics. The cycle
spreads that weight so posts stay varied instead of clumping one industry
and then ignoring it.
"""

from __future__ import annotations


def weighted_cycle(weights: dict[str, int]) -> list[str]:
    """Build one cycle that repeats each industry ``weight`` times, spread out.

    The highest-weight industry is the primary. Its slots are interleaved
    with a round-robin of the others, in dict order. For weights
    ``{fedramp: 3, aerospace_defense: 1, tech: 1, aws_cloud: 1}`` the cycle is
    fedramp, aerospace, fedramp, tech, fedramp, aws.
    """
    positive = [(name, weight) for name, weight in weights.items() if weight > 0]
    if not positive:
        return []

    primary_index = max(range(len(positive)), key=lambda index: positive[index][1])
    primary, primary_count = positive[primary_index]
    others: list[str] = []
    for index, (name, weight) in enumerate(positive):
        if index == primary_index:
            continue
        others.extend([name] * weight)
    if not others:
        return [primary] * primary_count

    cycle: list[str] = []
    primary_at = 0
    other_at = 0
    while primary_at < primary_count or other_at < len(others):
        if primary_at >= primary_count:
            cycle.append(others[other_at])
            other_at += 1
            continue
        if other_at >= len(others):
            cycle.append(primary)
            primary_at += 1
            continue
        primary_pos = (primary_at + 0.5) / primary_count
        other_pos = (other_at + 0.5) / len(others)
        if primary_pos <= other_pos:
            cycle.append(primary)
            primary_at += 1
        else:
            cycle.append(others[other_at])
            other_at += 1
    return cycle


def choose_industry(
    weights: dict[str, int],
    posted_industries: list[str],
    available: set[str],
) -> str | None:
    """Pick the next industry that has something new to post.

    The slot is ``len(posted) % cycle``. If that industry has no unseen item,
    walk forward. Avoid repeating the previous industry when another available
    industry is still in the cycle.
    """
    # Weights of industries with nothing new stay in the cycle so the phase
    # does not collapse onto whoever happens to have a fresh item.
    cycle = weighted_cycle(weights)
    if not cycle or not available:
        return None

    start = len(posted_industries) % len(cycle)
    last = posted_industries[-1] if posted_industries else None
    fallback: str | None = None
    for offset in range(len(cycle)):
        candidate = cycle[(start + offset) % len(cycle)]
        if candidate not in available:
            continue
        if fallback is None:
            fallback = candidate
        if candidate == last and any(name != last and name in available for name in cycle):
            continue
        return candidate
    return fallback
