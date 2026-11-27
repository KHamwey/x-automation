"""Verify @mentions against the X user directory before they are posted.

The draft step may name a public figure or organization. A handle is added
only when a user lookup on the same X client used for posting returns one
verified account whose display name matches that subject. A guessed handle
is ignored. Lookup errors, parody accounts, and ambiguous matches tag nobody.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from x_automation.config import apply_auth_headers

# GET /2/users/search accepts a short display-name query, not an arbitrary string.
_QUERY_EXTRA = " _'.-@&\""
_VERIFIED_TYPES = {"blue", "business", "government"}
_ALLOWED_KINDS = {
    "person",
    "public_figure",
    "publicfigure",
    "organization",
    "organisation",
    "org",
    "company",
    "agency",
}
_BLOCKED_KINDS = {
    "private",
    "private_individual",
    "privateindividual",
    "victim",
    "minor",
    "child",
    "witness",
    "unknown",
}
_BLOCKED_NAME = re.compile(
    r"\b(victims?|minors?|children|child|kids?|teens?|teenagers?|witness(?:es)?|"
    r"unidentified|anonymous|bystanders?|private citizens?)\b",
    re.IGNORECASE,
)
_AGE = re.compile(r"\b\d{1,2}\s*-?\s*years?\s*-?\s*old\b", re.IGNORECASE)
_GENERIC_SUBJECTS = {
    "officials",
    "police",
    "sources",
    "experts",
    "residents",
    "people",
    "government",
    "media",
    "voters",
    "authorities",
    "man",
    "woman",
}
_USERNAME = re.compile(r"^[A-Za-z0-9_]{1,20}$")
USER_FIELDS = ["name", "username", "verified", "verified_type", "parody", "protected"]


@dataclass(frozen=True)
class MentionCandidate:
    name: str
    kind: str


def normalize_display_name(value: str) -> str:
    """Compare names without case, accents, emoji, or punctuation."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def names_match(subject: str, display_name: str) -> bool:
    left = normalize_display_name(subject)
    right = normalize_display_name(display_name)
    return bool(left) and left == right


def mention_allowed(name: str, kind: str) -> bool:
    """Public figures and organizations only. Everyone else is skipped."""
    kind_key = re.sub(r"[\s\-]+", "_", (kind or "").strip().lower())
    if kind_key in _BLOCKED_KINDS or kind_key not in _ALLOWED_KINDS:
        return False
    cleaned = (name or "").strip()
    if not cleaned or _BLOCKED_NAME.search(cleaned) or _AGE.search(cleaned):
        return False
    if normalize_display_name(cleaned) in _GENERIC_SUBJECTS:
        return False
    return True


def is_acceptable_account(user: dict[str, Any]) -> bool:
    """Verified accounts only. Parody accounts are never tagged."""
    if user.get("parody") is True:
        return False
    if user.get("verified") is True:
        return True
    verified_type = str(user.get("verified_type") or "").strip().lower()
    return verified_type in _VERIFIED_TYPES


def search_query(name: str) -> str | None:
    chars: list[str] = []
    for ch in (name or "").strip():
        if ch.isalnum() or ch in _QUERY_EXTRA:
            chars.append(ch)
        else:
            chars.append(" ")
    text = re.sub(r"\s+", " ", "".join(chars)).strip()
    if not text:
        return None
    return text[:50]


def _first_page(result: Any) -> Any:
    if result is None or isinstance(result, (dict, list, str, bytes)):
        return result
    iterator = iter(result)
    page = next(iterator, None)
    close = getattr(iterator, "close", None)
    if close:
        close()
    return page


def _coerce_user(item: Any) -> dict[str, Any] | None:
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        dumped = item.model_dump()
        return dumped if isinstance(dumped, dict) else None
    if item is None:
        return None
    return {
        "id": getattr(item, "id", None),
        "name": getattr(item, "name", None),
        "username": getattr(item, "username", None),
        "verified": getattr(item, "verified", None),
        "verified_type": getattr(item, "verified_type", None),
        "parody": getattr(item, "parody", None),
        "protected": getattr(item, "protected", None),
    }


def _coerce_users(page: Any) -> list[dict[str, Any]]:
    if page is None:
        return []
    if isinstance(page, list):
        data: Any = page
    elif hasattr(page, "model_dump"):
        dumped = page.model_dump()
        data = dumped.get("data") if isinstance(dumped, dict) else None
    elif isinstance(page, dict):
        data = page.get("data")
    else:
        data = getattr(page, "data", None)
    if not isinstance(data, list):
        return []
    users: list[dict[str, Any]] = []
    for item in data or []:
        user = _coerce_user(item)
        if user:
            users.append(user)
    return users


def search_users(client: Any, name: str) -> list[dict[str, Any]]:
    """First page of ``GET /2/users/search`` for a display name.

    Uses the xdk client already used for posting (``client.users.search``).
    Only the first page is read so a broad name cannot walk the directory.
    """
    query = search_query(name)
    if client is None or query is None:
        return []
    apply_auth_headers(client)
    result = client.users.search(
        query=query,
        max_results=10,
        user_fields=USER_FIELDS,
    )
    return _coerce_users(_first_page(result))


def _candidate_fields(candidate: Any) -> tuple[str, str]:
    if isinstance(candidate, dict):
        name = candidate.get("name") or ""
        kind = candidate.get("kind") or candidate.get("type") or ""
        return str(name), str(kind)
    return str(getattr(candidate, "name", "") or ""), str(getattr(candidate, "kind", "") or "")


def _matching_username(users: list[dict[str, Any]], name: str) -> str | None:
    matches: list[str] = []
    seen: set[str] = set()
    for user in users:
        username = str(user.get("username") or "").strip().lstrip("@")
        if not username or not _USERNAME.fullmatch(username):
            continue
        if not names_match(name, str(user.get("name") or "")):
            continue
        if not is_acceptable_account(user):
            continue
        identity = str(user.get("id") or username).casefold()
        if identity in seen:
            continue
        seen.add(identity)
        matches.append(username)
    if len(matches) != 1:
        return None
    return matches[0]


def verify_mentions(
    candidates: list[Any] | None,
    *,
    client: Any,
    max_mentions: int = 2,
) -> list[str]:
    """Return up to two ``@handles`` that the X API actually confirms.

    Each subject is looked up by display name. The handle on the candidate,
    if one was supplied, is not used. No client, a failed request, a display
    name that does not match, an unverified account, or more than one
    verified match produces no tag for that subject.
    """
    if client is None:
        return []
    try:
        cap = max(0, min(2, int(max_mentions)))
    except (TypeError, ValueError):
        cap = 2
    handles: list[str] = []
    seen: set[str] = set()
    for candidate in candidates or []:
        if len(handles) >= cap:
            break
        name, kind = _candidate_fields(candidate)
        if not mention_allowed(name, kind):
            continue
        try:
            users = search_users(client, name)
        except Exception:
            continue
        username = _matching_username(users, name)
        if not username:
            continue
        key = username.casefold()
        if key in seen:
            continue
        seen.add(key)
        handles.append(f"@{username}")
    return handles
