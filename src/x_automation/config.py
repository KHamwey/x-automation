"""Configuration, paths, and client factory."""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from xdk import Client

SCOPES = ["tweet.read", "tweet.write", "users.read", "offline.access"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
TOKEN_PATH = DATA_DIR / "token.json"
CHECKPOINT_PATH = DATA_DIR / "checkpoint.json"
TAGS_PATH = DATA_DIR / "tags.json"

DEFAULT_API_BASE_URL = "https://api.x.com"

# Cost estimates per resource (USD) for summary output
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
    TOKEN_PATH.chmod(0o600)


def create_client(base_url: str | None = None) -> Client:
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
    """Ensure the session has a valid Authorization header."""
    if client.access_token:
        if client.oauth2_auth and client.token and client.is_token_expired():
            client.refresh_token()
            if client.token:
                save_token(client.token)
        client.session.headers["Authorization"] = f"Bearer {client.access_token}"
    elif client.bearer_token:
        client.session.headers["Authorization"] = f"Bearer {client.bearer_token}"
