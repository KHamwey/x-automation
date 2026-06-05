"""OAuth 2.0 PKCE authentication setup and token persistence."""

from __future__ import annotations

import webbrowser

from xdk import Client
from xdk.oauth2_auth import OAuth2PKCEAuth

from x_automation.config import (
    SCOPES,
    ensure_data_dir,
    get_env,
    load_env,
    save_token,
)


def run_auth_setup() -> None:
    """Interactive one-time OAuth2 PKCE flow; saves token to data/token.json."""
    load_env()
    client_id = get_env("CLIENT_ID")
    client_secret = get_env("CLIENT_SECRET")
    redirect_uri = get_env("REDIRECT_URI", "http://127.0.0.1:8080/callback")

    if not client_id:
        raise RuntimeError(
            "CLIENT_ID is required. Copy .env.example to .env and fill in credentials."
        )

    auth = OAuth2PKCEAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=SCOPES,
    )

    auth_url = auth.get_authorization_url()
    print("Visit this URL to authorize the app for your X account:")
    print(auth_url)
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    callback_url = input("\nPaste the full callback URL here: ").strip()
    if not callback_url:
        raise RuntimeError("No callback URL provided.")

    tokens = auth.fetch_token(authorization_response=callback_url)
    ensure_data_dir()
    save_token(tokens)

    client = Client(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        token=tokens,
        scope=SCOPES,
    )
    me = client.users.get_me()
    user = _extract_data(me)
    username = user.get("username", "unknown")
    user_id = user.get("id", "unknown")
    print(f"\nAuthenticated as @{username} (id: {user_id})")
    print(f"Token saved to data/token.json")


def _extract_data(response) -> dict:
    if isinstance(response, dict):
        return response.get("data") or {}
    data = getattr(response, "data", None)
    if data is None:
        return {}
    if isinstance(data, dict):
        return data
    if hasattr(data, "model_dump"):
        return data.model_dump()
    if hasattr(data, "__dict__"):
        return {k: v for k, v in data.__dict__.items() if not k.startswith("_")}
    return {}
