"""LinkedIn OAuth token load, save, and expiry.

The token file lives under ``data/`` (gitignored). GitHub Actions passes the
same JSON in ``LINKEDIN_TOKEN_JSON``. Neither path is committed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from x_automation.config import ensure_data_dir, get_env

from x_automation.linkedin.errors import LinkedInAuthError

# Imported lazily inside functions that need the path so tests can monkeypatch
# the module attribute after import.


def _token_path():
    from x_automation.linkedin.config import LINKEDIN_TOKEN_PATH

    return LINKEDIN_TOKEN_PATH


def parse_datetime(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def apply_token_payload(
    existing: dict[str, Any] | None,
    payload: dict[str, Any],
    *,
    obtained_at: datetime | None = None,
) -> dict[str, Any]:
    """Merge a token-endpoint response into the stored token record."""
    if not payload.get("access_token"):
        raise LinkedInAuthError(
            "LinkedIn token response did not include an access_token. "
            "Re-run linkedin auth."
        )
    when = (obtained_at or utc_now()).astimezone(timezone.utc).replace(microsecond=0)
    updated: dict[str, Any] = {}
    if existing:
        updated.update({key: value for key, value in existing.items() if not str(key).startswith("_")})
    updated["access_token"] = payload["access_token"]
    if payload.get("expires_in") is not None:
        expires_in = int(payload["expires_in"])
    elif existing and existing.get("expires_in"):
        expires_in = int(existing["expires_in"])
    else:
        expires_in = 0
    updated["expires_in"] = expires_in
    updated["obtained_at"] = when.isoformat()
    if expires_in:
        updated["expires_at"] = (when + timedelta(seconds=expires_in)).isoformat()
    if payload.get("refresh_token"):
        updated["refresh_token"] = payload["refresh_token"]
    if payload.get("refresh_token_expires_in"):
        refresh_in = int(payload["refresh_token_expires_in"])
        updated["refresh_token_expires_in"] = refresh_in
        updated["refresh_token_expires_at"] = (when + timedelta(seconds=refresh_in)).isoformat()
    if payload.get("scope"):
        updated["scope"] = payload["scope"]
    if existing and existing.get("_source"):
        updated["_source"] = existing["_source"]
    return updated


def access_token_expiry(token: dict[str, Any]) -> datetime | None:
    raw = token.get("expires_at")
    if not raw and token.get("obtained_at") and token.get("expires_in"):
        obtained = parse_datetime(str(token["obtained_at"]))
        return obtained + timedelta(seconds=int(token["expires_in"]))
    if not raw:
        return None
    return parse_datetime(str(raw))


def refresh_token_usable(token: dict[str, Any], now: datetime | None = None) -> bool:
    if not token.get("refresh_token"):
        return False
    raw = token.get("refresh_token_expires_at")
    if not raw:
        return True
    moment = now or utc_now()
    return parse_datetime(str(raw)) > moment


def expiry_warning(token: dict[str, Any], now: datetime | None = None) -> str | None:
    """Warn when a token with no refresh token is inside its last week."""
    if token.get("refresh_token"):
        return None
    expires = access_token_expiry(token)
    if expires is None:
        return None
    moment = now or utc_now()
    remaining = expires - moment
    if remaining.total_seconds() <= 0:
        return None
    if remaining <= timedelta(days=7):
        days = remaining.total_seconds() / 86400
        return (
            f"WARNING: LinkedIn access token expires in {days:.1f} days and this "
            "app did not receive a refresh token. Re-run "
            "`python -m x_automation.cli linkedin auth` and update the "
            "LINKEDIN_TOKEN_JSON secret before publishing starts failing."
        )
    return None


def load_linkedin_token() -> dict[str, Any]:
    raw = get_env("LINKEDIN_TOKEN_JSON")
    if raw and raw.strip():
        try:
            token = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LinkedInAuthError(
                "LINKEDIN_TOKEN_JSON is not valid JSON. Set that GitHub secret "
                "to the full contents of data/linkedin_token.json from "
                "`linkedin auth`. Do not commit the file."
            ) from exc
        if not isinstance(token, dict) or not token.get("access_token"):
            raise LinkedInAuthError(
                "LINKEDIN_TOKEN_JSON has no access_token. Paste the full "
                "data/linkedin_token.json file into that secret."
            )
        token["_source"] = "env"
        return token

    path = _token_path()
    if path.exists():
        token = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(token, dict) or not token.get("access_token"):
            raise LinkedInAuthError(
                f"{path} has no access_token. Run: python -m x_automation.cli linkedin auth"
            )
        token["_source"] = "file"
        return token

    raise LinkedInAuthError(
        "No LinkedIn token found. Locally, run "
        "`python -m x_automation.cli linkedin auth`. In GitHub Actions, set "
        "the LINKEDIN_TOKEN_JSON secret to the contents of data/linkedin_token.json."
    )


def save_linkedin_token(token: dict[str, Any]) -> None:
    ensure_data_dir()
    path = _token_path()
    clean = {key: value for key, value in token.items() if not str(key).startswith("_")}
    path.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def blank_to_none(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    return str(value).strip()
