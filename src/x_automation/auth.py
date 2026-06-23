"""OAuth 2.0 PKCE authentication setup and token persistence.

X API v2 uses OAuth 2.0 with PKCE for user-context requests (reading and
deleting your own tweets). This module runs the one-time browser flow and
saves the resulting access + refresh tokens to ``data/token.json``.

The ``offline.access`` scope (configured in :mod:`x_automation.config`) is
required so the refresh token lets long delete runs survive token expiry
without re-authorizing in a browser.
"""

from __future__ import annotations

import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

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
    print("Authorize the app for your X account in your browser.")
    print(f"If it does not open automatically, visit:\n{auth_url}\n")

    callback_url: str | None = None
    try:
        callback_url = _wait_for_callback(redirect_uri, auth_url=auth_url, timeout=300)
        print("Authorization code received.")
    except (OSError, TimeoutError) as exc:
        print(f"\nLocal callback server unavailable ({exc}).")
        print(_manual_paste_instructions(redirect_uri))
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass
        callback_url = _prompt_for_callback_url()

    if not callback_url:
        raise RuntimeError("No callback URL provided.")

    _validate_callback_url(callback_url)

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
    print("Token saved to data/token.json")


def _wait_for_callback(
    redirect_uri: str,
    *,
    auth_url: str,
    timeout: float = 300,
) -> str:
    """Run a temporary local server to capture the OAuth redirect."""
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    expected_path = parsed.path or "/"
    scheme = parsed.scheme or "http"

    state: dict[str, str | None] = {"url": None, "error": None}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            request = urlparse(self.path)
            if request.path != expected_path:
                self.send_error(404)
                return

            query = parse_qs(request.query)
            if "error" in query:
                state["error"] = query.get("error_description", query["error"])[0]
            elif "code" not in query:
                state["error"] = "No authorization code in callback URL"
            else:
                state["url"] = f"{scheme}://{host}:{port}{self.path}"

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if state["error"]:
                body = (
                    "<h1>Authorization failed</h1>"
                    f"<p>{state['error']}</p>"
                    "<p>Return to the terminal.</p>"
                )
            else:
                body = (
                    "<h1>Authorization complete</h1>"
                    "<p>You can close this tab and return to the terminal.</p>"
                )
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, format: str, *args) -> None:
            return

    server = HTTPServer((host, port), CallbackHandler)
    server.timeout = 1

    print(f"Waiting for callback on http://{host}:{port} ...")

    def open_browser() -> None:
        time.sleep(0.5)
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

    threading.Thread(target=open_browser, daemon=True).start()

    deadline = time.time() + timeout
    while state["url"] is None and state["error"] is None and time.time() < deadline:
        server.handle_request()
    server.server_close()

    if state["error"]:
        raise RuntimeError(f"OAuth error: {state['error']}")
    if not state["url"]:
        raise TimeoutError(
            "Timed out waiting for authorization. Approve the app in the browser, then try again."
        )
    return state["url"]


def _manual_paste_instructions(redirect_uri: str) -> str:
    return (
        "\nManual fallback:\n"
        "1. Authorize in the browser.\n"
        "2. X redirects to your callback URL. The page may show 'connection refused' — "
        "that is OK.\n"
        "3. Copy the FULL URL from the address bar (must include ?code=...).\n"
        f"   Example: {redirect_uri}?code=XXXX&state=YYYY\n"
    )


def _prompt_for_callback_url() -> str:
    return input("\nPaste the full callback URL here: ").strip()


def _validate_callback_url(url: str) -> None:
    if "code=" not in url:
        raise RuntimeError(
            "That URL has no ?code= parameter.\n\n"
            "You pasted the redirect URI only. After authorizing on X, copy the "
            "entire address bar URL, for example:\n"
            "  http://127.0.0.1:8080/callback?code=...&state=...\n\n"
            "The browser may show 'connection refused' after redirect — the code "
            "is still in the address bar. Copy it from there.\n\n"
            "Re-run: python -m x_automation.cli auth"
        )


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
