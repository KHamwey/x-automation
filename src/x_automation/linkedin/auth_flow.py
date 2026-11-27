"""One-time local LinkedIn OAuth 2.0 authorization-code flow.

LinkedIn calls this the 3-legged OAuth flow. It is interactive and stores
``data/linkedin_token.json``. GitHub Actions never runs this command; it
reads the token from the ``LINKEDIN_TOKEN_JSON`` secret.
"""

from __future__ import annotations

import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx

from x_automation.config import get_env, load_env
from x_automation.linkedin.client import AUTH_URL, TOKEN_URL, LinkedInClient
from x_automation.linkedin.config import (
    DEFAULT_REDIRECT_URI,
    LINKEDIN_SCOPES,
    api_version,
)
from x_automation.linkedin.errors import LinkedInAuthError
from x_automation.linkedin.tokens import apply_token_payload, save_linkedin_token

SCOPE_STRING = " ".join(LINKEDIN_SCOPES)


def build_authorization_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": SCOPE_STRING,
        },
        quote_via=quote,
    )
    return f"{AUTH_URL}?{query}"


def exchange_authorization_code(
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
    http_client: httpx.Client,
) -> dict[str, Any]:
    response = http_client.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    if response.status_code >= 400:
        raise LinkedInAuthError(
            "LinkedIn token exchange failed "
            f"({response.status_code}): {response.text[:400]}"
        )
    return apply_token_payload(None, response.json())


def parse_callback_url(url: str) -> tuple[str, str | None]:
    query = parse_qs(urlparse(url).query)
    if "error" in query:
        description = query.get("error_description") or query.get("error") or ["unknown"]
        raise RuntimeError(f"LinkedIn authorization failed: {description[0]}")
    code = (query.get("code") or [None])[0]
    if not code:
        raise RuntimeError(
            "That URL has no ?code= parameter. After approving the app, copy "
            "the full address bar URL, including ?code=...&state=..."
        )
    state = (query.get("state") or [None])[0]
    return code, state


def run_linkedin_auth() -> None:
    """Interactive browser flow. Saves data/linkedin_token.json."""
    load_env()
    client_id = (get_env("LINKEDIN_CLIENT_ID") or "").strip()
    client_secret = (get_env("LINKEDIN_CLIENT_SECRET") or "").strip()
    redirect_uri = (get_env("LINKEDIN_REDIRECT_URI") or DEFAULT_REDIRECT_URI).strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            "LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET are required. "
            "Copy .env.example to .env and fill in the LinkedIn app credentials."
        )

    state = secrets.token_urlsafe(24)
    auth_url = build_authorization_url(
        client_id=client_id, redirect_uri=redirect_uri, state=state
    )
    print("Authorize the LinkedIn app in your browser.")
    print("Requested scopes: openid profile w_member_social")
    print(f"Redirect URI (must match the app Auth tab exactly):\n  {redirect_uri}\n")
    print(f"If the browser does not open, visit:\n{auth_url}\n")

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
        callback_url = input("\nPaste the full callback URL here: ").strip()

    code, returned_state = parse_callback_url(callback_url)
    if returned_state != state:
        raise RuntimeError("OAuth state did not match. Run linkedin auth again.")

    with httpx.Client(timeout=30.0) as http:
        token = exchange_authorization_code(
            code=code,
            redirect_uri=redirect_uri,
            client_id=client_id,
            client_secret=client_secret,
            http_client=http,
        )
        client = LinkedInClient(
            token,
            api_version=api_version(),
            http_client=http,
            client_id=client_id,
            client_secret=client_secret,
        )
        profile = client.get_userinfo()

    token["member_id"] = profile["sub"]
    if profile.get("name"):
        token["member_name"] = profile["name"]
    save_linkedin_token(token)
    _print_success(token)


def _print_success(token: dict[str, Any]) -> None:
    name = token.get("member_name") or "unknown"
    member_id = token.get("member_id")
    print(f"\nAuthenticated as {name} (urn:li:person:{member_id})")
    print("Token saved to data/linkedin_token.json")
    print(f"Access token expires at {token.get('expires_at', 'unknown')} (about 60 days).")
    if token.get("refresh_token"):
        print(
            "Refresh token: granted, expires at "
            f"{token.get('refresh_token_expires_at', 'unknown')}."
        )
        print(
            "Set LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET in GitHub Actions "
            "so the workflow can refresh this token."
        )
    else:
        print(
            "Refresh token: not granted. LinkedIn only returns one when the app "
            "is approved for programmatic refresh. Re-run linkedin auth when the "
            "access token expires, then update the GitHub secret."
        )
    print("\nSet the GitHub secret to this file (the file stays gitignored):")
    print("  gh secret set LINKEDIN_TOKEN_JSON < data/linkedin_token.json")
    print("  gh secret set XAI_API_KEY")
    print("  gh secret set LINKEDIN_CLIENT_ID")
    print("  gh secret set LINKEDIN_CLIENT_SECRET")
    print("\nDo not commit data/linkedin_token.json.")


def _manual_paste_instructions(redirect_uri: str) -> str:
    return (
        "\nManual fallback:\n"
        "1. Approve the app in the browser.\n"
        "2. LinkedIn redirects to the callback URL. The page may show "
        "'connection refused' — that is OK.\n"
        "3. Copy the FULL URL from the address bar (it must include ?code=).\n"
        f"   Example: {redirect_uri}?code=XXXX&state=YYYY\n"
    )


def _wait_for_callback(redirect_uri: str, *, auth_url: str, timeout: float) -> str:
    parsed = urlparse(redirect_uri)
    hostname = parsed.hostname or "127.0.0.1"
    bind_host = "127.0.0.1" if hostname == "localhost" else hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    expected_path = parsed.path or "/"
    state: dict[str, str | None] = {"url": None, "error": None}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
            request = urlparse(self.path)
            if request.path != expected_path:
                self.send_error(404)
                return
            query = parse_qs(request.query)
            if "error" in query:
                state["error"] = (query.get("error_description") or query["error"])[0]
            elif "code" not in query:
                state["error"] = "No authorization code in callback URL"
            else:
                state["url"] = f"{parsed.scheme}://{hostname}:{port}{self.path}"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if state["error"]:
                body = (
                    "<h1>Authorization failed</h1>"
                    f"<p>{state['error']}</p><p>Return to the terminal.</p>"
                )
            else:
                body = (
                    "<h1>LinkedIn authorization complete</h1>"
                    "<p>You can close this tab and return to the terminal.</p>"
                )
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer((bind_host, port), CallbackHandler)
    server.timeout = 1
    print(f"Waiting for callback on http://{bind_host}:{port} ...")

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
            "Timed out waiting for LinkedIn authorization. Approve the app, then try again."
        )
    return state["url"]
