"""OAuth 2.0 PKCE authentication setup and token persistence.

X API v2 uses OAuth 2.0 with PKCE for user-context requests (reading and
deleting your own tweets). This module runs the one-time browser flow and
saves the resulting access + refresh tokens to ``data/token.json``.

The ``offline.access`` scope (configured in :mod:`x_automation.config`) is
required so the refresh token lets long delete runs survive token expiry
without re-authorizing in a browser.
"""

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

    # PKCE flow: no client secret required for public/native apps, but the
    # X Developer Console may still issue one — pass it if present.
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
        # Headless or restricted environments — user can paste the URL manually.
        pass

    # After authorizing, X redirects to redirect_uri with a ?code= param.
    # The user must paste the full callback URL because we don't run a local server.
    callback_url = input("\nPaste the full callback URL here: ").strip()
    if not callback_url:
        raise RuntimeError("No callback URL provided.")

    tokens = auth.fetch_token(authorization_response=callback_url)
    ensure_data_dir()
    save_token(tokens)

    # Verify the token works before the user spends credits on a delete run.
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
    """Normalize xdk response objects (dict, Pydantic model, or plain object) to a dict."""
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
