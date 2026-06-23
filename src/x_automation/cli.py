"""Command-line interface for x-automation.

Subcommands:

- ``auth`` — one-time OAuth2 PKCE setup (no API credits consumed beyond a
  single GET /2/users/me verification call).
- ``delete-all`` — fetch and optionally delete every post the authenticated
  user authored (original tweets, replies, and quote/repost records).
- ``delete`` — same flow, but only posts whose text contains a given emoji tag.

Safety defaults: delete commands are **dry-run** unless ``--execute`` is passed.
Even with ``--execute``, the delete module prompts for a typed confirmation
before any DELETE /2/tweets/:id calls are made.
"""

from __future__ import annotations

import argparse
import sys

from x_automation.auth import run_auth_setup
from x_automation.config import DEFAULT_API_BASE_URL, create_client
from x_automation.delete import print_summary, run_delete
from x_automation.filters import resolve_emoji_filter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="x-automation",
        description="Programmatic tools for managing your X account.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth_parser = subparsers.add_parser(
        "auth",
        help="Run one-time OAuth2 PKCE setup and save token",
    )
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
    delete_filtered.add_argument(
        "--emoji",
        help="Emoji tag to match in post text (e.g. '🟦')",
    )
    delete_filtered.add_argument(
        "--tag",
        help="Human-readable tag alias from data/tags.json",
    )
    delete_filtered.set_defaults(func=cmd_delete_filtered)

    return parser


def _add_delete_flags(parser: argparse.ArgumentParser) -> None:
    # --execute is opt-in: default dry-run only performs owned reads (GET
    # /2/users/:id/tweets), which still costs credits but cannot delete anything.
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete posts (default is dry-run)",
    )
    parser.add_argument(
        "--api-base-url",
        default=None,
        help=f"API base URL (default: {DEFAULT_API_BASE_URL} or API_BASE_URL env)",
    )
    # --resume skips post IDs already recorded in data/checkpoint.json so a
    # long delete run can continue after Ctrl-C or rate-limit exhaustion.
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from data/checkpoint.json",
    )


def cmd_auth(_args: argparse.Namespace) -> int:
    run_auth_setup()
    return 0


def cmd_delete_all(args: argparse.Namespace) -> int:
    return _run_delete_command(args, emoji_filter=None)


def cmd_delete_filtered(args: argparse.Namespace) -> int:
    if not args.emoji and not args.tag:
        print("Error: --emoji or --tag is required for delete command.", file=sys.stderr)
        return 1
    try:
        emoji_filter = resolve_emoji_filter(args.emoji, args.tag)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return _run_delete_command(args, emoji_filter=emoji_filter)


def _run_delete_command(args: argparse.Namespace, emoji_filter: str | None) -> int:
    execute = args.execute
    try:
        client = create_client(base_url=args.api_base_url)
        result = run_delete(
            client,
            execute=execute,
            resume=args.resume,
            emoji_filter=emoji_filter,
        )
        print_summary(result)
        return 0 if result.errors == 0 else 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
