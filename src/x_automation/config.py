"""Configuration, paths, and client factory.

Loads credentials from ``.env`` (via python-dotenv) and persisted OAuth
tokens from ``data/token.json``. All runtime artifacts (token, checkpoints,
inventories) live under ``data/`` which is gitignored.

Cost constants below are rough USD estimates for console output only — confirm
current pay-per-use rates in the X Developer Console before running against
production.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from xdk import Client

# Scopes required for the full delete workflow:
#   tweet.read  — GET /2/users/:id/tweets (owned reads, billed per post returned)
#   tweet.write — DELETE /2/tweets/:id
#   users.read  — GET /2/users/me (resolve authenticated user ID)
#   offline.access — refresh token for multi-hour delete runs
SCOPES = ["tweet.read", "tweet.write", "users.read", "offline.access"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
TOKEN_PATH = DATA_DIR / "token.json"
CHECKPOINT_PATH = DATA_DIR / "checkpoint.json"
TAGS_PATH = DATA_DIR / "tags.json"

DEFAULT_API_BASE_URL = "https://api.x.com"

# Rough pay-per-use estimates (USD) shown in delete summaries.
# Owned reads (listing your tweets) and deletes are billed separately.
OWNED_READ_COST = 0.001
DELETE_COST_LOW = 0.005
DELETE_COST_HIGH = 0.010


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_env() -> None:
    load_dotenv(PROJECT_ROOT / ".env")


def get_env(name: str, default: str | None = None) -> str | None:
    load_env()
    return os.getenv(name, default)


def load_token() -> dict | None:
    if not TOKEN_PATH.exists():
        return None
    with TOKEN_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def save_token(token: dict) -> None:
    ensure_data_dir()
    with TOKEN_PATH.open("w", encoding="utf-8") as f:
        json.dump(token, f, indent=2)
    # Restrict permissions — token file contains refresh token secrets.
    TOKEN_PATH.chmod(0o600)


def create_client(base_url: str | None = None) -> Client:
    """Build an xdk Client from .env + saved OAuth token (or bearer fallback).

    Production: OAuth token from ``auth`` command (user-context, required for
    deleting your own tweets).

    Playground: set ``BEARER_TOKEN=test`` and ``--api-base-url http://localhost:8080``
    to exercise the CLI without spending credits or touching a real account.
    """
    load_env()
    token = load_token()
    client_id = get_env("CLIENT_ID")
    client_secret = get_env("CLIENT_SECRET")
    redirect_uri = get_env("REDIRECT_URI")

    if not token and not get_env("BEARER_TOKEN"):
        raise RuntimeError(
            "No saved token found. Run: python -m x_automation.cli auth"
        )

    resolved_base_url = base_url or get_env("API_BASE_URL", DEFAULT_API_BASE_URL)

    bearer = get_env("BEARER_TOKEN")
    if bearer and not token:
        return Client(base_url=resolved_base_url, bearer_token=bearer)

    if not client_id:
        raise RuntimeError("CLIENT_ID is required in .env")

    return Client(
        base_url=resolved_base_url,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        token=token,
        scope=SCOPES,
    )


def apply_auth_headers(client: Client) -> None:
    """Ensure the session has a valid Authorization header.

    Proactively refreshes expired OAuth tokens and persists the new token
    so a multi-hour delete run does not fail mid-batch.
    """
    if client.access_token:
        if client.oauth2_auth and client.token and client.is_token_expired():
            client.refresh_token()
            if client.token:
                save_token(client.token)
        client.session.headers["Authorization"] = f"Bearer {client.access_token}"
    elif client.bearer_token:
        client.session.headers["Authorization"] = f"Bearer {client.bearer_token}"
