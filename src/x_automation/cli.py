"""Command-line interface for x-automation."""

from __future__ import annotations

import argparse
import sys

from x_automation.auth import run_auth_setup
from x_automation.config import DEFAULT_API_BASE_URL, create_client
from x_automation.delete import print_summary, run_delete
from x_automation.drafts import approve_draft, get_draft, list_drafts, reject_draft
from x_automation.filters import load_tag_map, resolve_emoji_filter
from x_automation.ingest import print_ingest_summary, run_ingest
from x_automation.inbox import import_inbox
from x_automation.publish import daily_cap as get_daily_cap
from x_automation.publish import print_publish_summary, run_publish, stats_today


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="x-automation",
        description="Programmatic tools for managing your X account.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth_parser = subparsers.add_parser("auth", help="Run one-time OAuth2 PKCE setup")
    auth_parser.set_defaults(func=cmd_auth)

    delete_all = subparsers.add_parser(
        "delete-all",
        help="Delete all posts, replies, and reposts you authored",
    )
    _add_delete_flags(delete_all)
    delete_all.set_defaults(func=cmd_delete_all, emoji=None, tag=None)

    delete_filtered = subparsers.add_parser(
        "delete",
        help="Delete posts matching an emoji tag",
    )
    _add_delete_flags(delete_filtered)
    delete_filtered.add_argument("--emoji", help="Emoji tag to match in post text")
    delete_filtered.add_argument("--tag", help="Human-readable tag alias from data/tags.json")
    delete_filtered.set_defaults(func=cmd_delete_filtered)

    drafts_parser = subparsers.add_parser("drafts", help="Draft queue for approve-first posting")
    drafts_sub = drafts_parser.add_subparsers(dest="drafts_command", required=True)

    fetch_p = drafts_sub.add_parser("fetch", help="Poll RSS feeds and create Grok drafts")
    fetch_p.add_argument(
        "--no-grok",
        action="store_true",
        help="Skip Grok; use raw RSS titles (for testing)",
    )
    fetch_p.set_defaults(func=cmd_drafts_fetch)

    import_p = drafts_sub.add_parser(
        "import-inbox",
        help="Import JSON drafts from data/inbox/cursor/",
    )
    import_p.set_defaults(func=cmd_drafts_import_inbox)

    list_p = drafts_sub.add_parser("list", help="List drafts")
    list_p.add_argument(
        "--status",
        choices=["pending", "approved", "rejected", "published"],
        help="Filter by status",
    )
    list_p.set_defaults(func=cmd_drafts_list)

    show_p = drafts_sub.add_parser("show", help="Show a single draft")
    show_p.add_argument("draft_id", help="Draft UUID")
    show_p.set_defaults(func=cmd_drafts_show)

    approve_p = drafts_sub.add_parser("approve", help="Approve a draft for publishing")
    approve_p.add_argument("draft_id", help="Draft UUID")
    approve_p.set_defaults(func=cmd_drafts_approve)

    reject_p = drafts_sub.add_parser("reject", help="Reject a draft")
    reject_p.add_argument("draft_id", help="Draft UUID")
    reject_p.set_defaults(func=cmd_drafts_reject)

    publish_p = drafts_sub.add_parser("publish", help="Publish approved drafts")
    publish_p.add_argument("--execute", action="store_true", help="Post to X (default dry-run)")
    publish_p.add_argument("--id", dest="draft_id", help="Publish a single approved draft")
    publish_p.add_argument("--api-base-url", default=None)
    publish_p.set_defaults(func=cmd_drafts_publish)

    stats_p = drafts_sub.add_parser("stats", help="Show today's publish counts vs daily cap")
    stats_p.set_defaults(func=cmd_drafts_stats)

    return parser


def _add_delete_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--execute", action="store_true", help="Actually delete posts")
    parser.add_argument("--api-base-url", default=None)
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")


def cmd_auth(_args: argparse.Namespace) -> int:
    run_auth_setup()
    return 0


def cmd_delete_all(args: argparse.Namespace) -> int:
    return _run_delete_command(args, emoji_filter=None)


def cmd_delete_filtered(args: argparse.Namespace) -> int:
    if not args.emoji and not args.tag:
        print("Error: --emoji or --tag is required.", file=sys.stderr)
        return 1
    try:
        emoji_filter = resolve_emoji_filter(args.emoji, args.tag)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return _run_delete_command(args, emoji_filter=emoji_filter)


def _run_delete_command(args: argparse.Namespace, emoji_filter: str | None) -> int:
    try:
        client = create_client(base_url=args.api_base_url)
        result = run_delete(
            client,
            execute=args.execute,
            resume=args.resume,
            emoji_filter=emoji_filter,
        )
        print_summary(result)
        return 0 if result.errors == 0 else 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_drafts_fetch(args: argparse.Namespace) -> int:
    try:
        result = run_ingest(use_grok=not args.no_grok)
        print_ingest_summary(result)
        return 0 if result.errors == 0 else 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_drafts_import_inbox(_args: argparse.Namespace) -> int:
    try:
        tag_map = load_tag_map()
        result = import_inbox(tag_map=tag_map)
        print(f"\nImported: {result.imported}, errors: {result.errors}")
        for detail in result.error_details:
            print(f"  - {detail}")
        return 0 if result.errors == 0 else 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_drafts_list(args: argparse.Namespace) -> int:
    drafts = list_drafts(args.status)
    if not drafts:
        print("No drafts found.")
        return 0
    print(f"\n{'ID':<38} {'Status':<10} {'Cat':<14} {'Cost':<6} Preview")
    print("-" * 100)
    for draft in drafts:
        preview = (draft.get("text") or "").replace("\n", " ")[:40]
        cost = draft.get("estimated_x_cost_usd", 0)
        print(
            f"{draft.get('id', ''):<38} "
            f"{draft.get('status', ''):<10} "
            f"{draft.get('category', ''):<14} "
            f"${cost:<5.2f} "
            f"{preview}"
        )
    return 0


def cmd_drafts_show(args: argparse.Namespace) -> int:
    draft = get_draft(args.draft_id)
    if not draft:
        print(f"Draft not found: {args.draft_id}", file=sys.stderr)
        return 1
    for key, value in draft.items():
        print(f"{key}: {value}")
    return 0


def cmd_drafts_approve(args: argparse.Namespace) -> int:
    try:
        draft = approve_draft(args.draft_id)
        print(f"Approved {draft['id']} [{draft.get('category')}]")
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_drafts_reject(args: argparse.Namespace) -> int:
    try:
        draft = reject_draft(args.draft_id)
        print(f"Rejected {draft['id']}")
        return 0
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_drafts_publish(args: argparse.Namespace) -> int:
    try:
        client = create_client(base_url=args.api_base_url)
        result = run_publish(
            client,
            execute=args.execute,
            draft_id=args.draft_id,
        )
        print_publish_summary(result, execute=args.execute)
        if not args.execute:
            print("\nNo posts published. Pass --execute to post to X.")
        return 0 if result.errors == 0 else 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_drafts_stats(_args: argparse.Namespace) -> int:
    cap = get_daily_cap()
    counts = stats_today()
    print(f"\nDaily publish cap: {cap} per category (UTC day)")
    if not counts:
        print("  No publishes today.")
    else:
        for category, count in sorted(counts.items()):
            print(f"  {category}: {count}/{cap}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
