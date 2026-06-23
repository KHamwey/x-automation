"""Delete posts with dry-run support, checkpoints, and cost estimates.

Orchestrates the full delete workflow:

1. Resolve authenticated user (GET /2/users/me)
2. Paginate timeline (GET /2/users/:id/tweets) — billed as owned reads
3. Save inventory JSON for manual review (always, even in dry-run)
4. Preview posts in the terminal
5. On ``--execute``: typed confirmation, then DELETE /2/tweets/:id per post

Checkpoints (``data/checkpoint.json``) record successfully deleted IDs so
``--resume`` can skip them after an interruption. Large histories are limited
to ~45 deletes per 15 minutes by :mod:`x_automation.rate_limit`, so a 5k-post
cleanup can take many hours.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from xdk import Client

from x_automation.config import (
    CHECKPOINT_PATH,
    DELETE_COST_HIGH,
    DELETE_COST_LOW,
    OWNED_READ_COST,
    apply_auth_headers,
    ensure_data_dir,
)
from x_automation.rate_limit import RateLimiter
from x_automation.timeline import (
    fetch_user_posts,
    get_authenticated_user,
    preview_posts,
    save_inventory,
)


@dataclass
class DeleteResult:
    fetched: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: int = 0
    error_details: list[str] = field(default_factory=list)
    inventory_path: str | None = None

    def estimated_cost(self) -> tuple[float, float]:
        """Return (low, high) USD estimate from fetched + deleted counts."""
        read_cost = self.fetched * OWNED_READ_COST
        delete_cost_low = self.deleted * DELETE_COST_LOW
        delete_cost_high = self.deleted * DELETE_COST_HIGH
        return read_cost + delete_cost_low, read_cost + delete_cost_high


def load_checkpoint() -> dict:
    if not CHECKPOINT_PATH.exists():
        return {"deleted_ids": []}
    with CHECKPOINT_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def save_checkpoint(deleted_ids: list[str], user_id: str, total: int) -> None:
    """Persist progress after each successful delete.

    Written incrementally (not batched) so Ctrl-C loses at most one in-flight
    delete, not the entire run.
    """
    ensure_data_dir()
    payload = {
        "user_id": user_id,
        "total_target": total,
        "deleted_ids": deleted_ids,
    }
    with CHECKPOINT_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def clear_checkpoint() -> None:
    if CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()


def confirm_execution(count: int, *, delete_all: bool) -> bool:
    """Require exact typed confirmation before irreversible deletes.

    ``delete-all`` expects ``DELETE ALL``; filtered deletes expect
    ``DELETE <count>`` matching the remaining post count so the user
    cannot confirm without reading how many posts will be removed.
    """
    if delete_all:
        prompt = "Type DELETE ALL to confirm permanent deletion: "
        expected = "DELETE ALL"
    else:
        prompt = f"Type DELETE {count} to confirm permanent deletion: "
        expected = f"DELETE {count}"

    answer = input(prompt).strip()
    return answer == expected


def delete_post(client: Client, post_id: str, rate_limiter: RateLimiter) -> None:
    """DELETE /2/tweets/:id for a single post (billed per delete)."""
    apply_auth_headers(client)
    url = f"{client.base_url}/2/tweets/{post_id}"
    response = rate_limiter.request(client.session, "DELETE", url, bucket="delete")
    # 404 = already gone (e.g. manual delete or prior partial run) — treat as success.
    if response.status_code == 404:
        return
    response.raise_for_status()


def run_delete(
    client: Client,
    *,
    execute: bool = False,
    resume: bool = False,
    emoji_filter: str | None = None,
) -> DeleteResult:
    rate_limiter = RateLimiter()
    result = DeleteResult()

    user = get_authenticated_user(client, rate_limiter)
    user_id = user.get("id")
    if not user_id:
        raise RuntimeError("Could not determine authenticated user ID.")

    username = user.get("username", "unknown")
    print(f"Authenticated as @{username} (id: {user_id})")

    filter_label = f" matching emoji '{emoji_filter}'" if emoji_filter else ""
    print(f"Fetching posts{filter_label}...")

    posts = fetch_user_posts(
        client,
        user_id,
        rate_limiter,
        emoji_filter=emoji_filter,
    )
    result.fetched = len(posts)
    print(f"Found {result.fetched} post(s).")

    if result.fetched == 0:
        return result

    # Always write inventory before any deletes — review this file in dry-run.
    result.inventory_path = save_inventory(posts, user)
    print(f"Inventory saved to {result.inventory_path}")

    preview_posts(posts)

    already_deleted: set[str] = set()
    if resume:
        checkpoint = load_checkpoint()
        already_deleted = set(checkpoint.get("deleted_ids") or [])
        if already_deleted:
            print(f"Resuming: {len(already_deleted)} post(s) already deleted.")

    to_delete = [p for p in posts if p.get("id") not in already_deleted]
    remaining = len(to_delete)

    low, high = result.estimated_cost()
    if execute:
        # Include projected delete cost for posts not yet removed.
        est_low = low + remaining * DELETE_COST_LOW
        est_high = high + remaining * DELETE_COST_HIGH
    else:
        est_low, est_high = low, high

    print(
        f"\nEstimated API cost: ${est_low:.2f} – ${est_high:.2f} "
        f"(reads @ ${OWNED_READ_COST}/post, deletes @ ${DELETE_COST_LOW}–${DELETE_COST_HIGH}/post)"
    )

    if not execute:
        print("\n[DRY RUN] No posts deleted. Pass --execute to delete.")
        return result

    if not confirm_execution(remaining, delete_all=emoji_filter is None):
        print("Aborted.")
        return result

    deleted_ids = list(already_deleted)
    for i, post in enumerate(to_delete, start=1):
        post_id = post.get("id")
        if not post_id:
            result.skipped += 1
            continue
        try:
            delete_post(client, post_id, rate_limiter)
            deleted_ids.append(post_id)
            result.deleted += 1
            save_checkpoint(deleted_ids, user_id, result.fetched)
            text_preview = (post.get("text") or "")[:50].replace("\n", " ")
            print(f"  [{i}/{remaining}] Deleted {post_id}: {text_preview}")
        except Exception as exc:
            result.errors += 1
            msg = f"{post_id}: {exc}"
            result.error_details.append(msg)
            print(f"  [ERROR] {msg}")

    if result.errors == 0 and result.deleted == remaining:
        clear_checkpoint()
        print("\nCheckpoint cleared (all targets deleted).")

    return result


def print_summary(result: DeleteResult) -> None:
    low, high = result.estimated_cost()
    print("\n--- Summary ---")
    print(f"  Fetched:  {result.fetched}")
    print(f"  Deleted:  {result.deleted}")
    print(f"  Skipped:  {result.skipped}")
    print(f"  Errors:   {result.errors}")
    if result.inventory_path:
        print(f"  Inventory: {result.inventory_path}")
    print(f"  Est. cost: ${low:.2f} – ${high:.2f}")
    if result.error_details:
        print("  Error details:")
        for detail in result.error_details[:10]:
            print(f"    - {detail}")
