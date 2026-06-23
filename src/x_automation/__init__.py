"""Tools for programmatic X account management.

This package provides a CLI for bulk post deletion with safety defaults:
dry-run by default, typed confirmation before writes, checkpoint resume,
and rate-limit pacing tuned for X API v2 pay-per-use billing.

Quick start::

    python -m x_automation.cli auth          # one-time OAuth2 PKCE setup
    python -m x_automation.cli delete-all   # dry-run (reads only, no deletes)
    python -m x_automation.cli delete-all --execute  # actually delete

See README.md for cost estimates, playground testing, and emoji-tag filtering.
"""

__version__ = "0.1.0"
