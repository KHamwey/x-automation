"""LinkedIn official API client: userinfo, Posts, and Images.

OAuth and posting follow the current member APIs:

- member id from ``GET https://api.linkedin.com/v2/userinfo`` (``sub``)
- create with ``POST https://api.linkedin.com/rest/posts``
- author ``urn:li:person:<id>``
- headers ``LinkedIn-Version`` (YYYYMM) and ``X-Restli-Protocol-Version: 2.0.0``

Image upload uses the Images API ``initializeUpload`` action. It is optional.
"""

from __future__ import annotations

from typing import Any, Callable

import httpx

from x_automation.config import get_env
from x_automation.linkedin.commentary import COMMENTARY_LIMIT, escape_little
from x_automation.linkedin.errors import LinkedInAuthError
from x_automation.linkedin.tokens import (
    access_token_expiry,
    apply_token_payload,
    blank_to_none,
    load_linkedin_token,
    refresh_token_usable,
    save_linkedin_token,
    utc_now,
)

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
POSTS_URL = "https://api.linkedin.com/rest/posts"
IMAGES_INIT_URL = "https://api.linkedin.com/rest/images?action=initializeUpload"
RESTLI_PROTOCOL = "2.0.0"
REFRESH_SKEW_SECONDS = 24 * 60 * 60


class LinkedInClient:
    def __init__(
        self,
        token: dict[str, Any],
        *,
        api_version: str,
        http_client: httpx.Client | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        on_token_update: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if not token.get("access_token"):
            raise LinkedInAuthError("LinkedIn token is missing access_token.")
        self.token = token
        self.api_version = api_version
        self.client_id = blank_to_none(client_id)
        self.client_secret = blank_to_none(client_secret)
        self.on_token_update = on_token_update
        self._http = http_client or httpx.Client(timeout=30.0, follow_redirects=True)
        self._owns_http = http_client is None

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    @property
    def access_token(self) -> str:
        token = self.token.get("access_token")
        if not token:
            raise LinkedInAuthError("LinkedIn token is missing access_token.")
        return str(token)

    def author_urn(self) -> str:
        member_id = self.token.get("member_id")
        if not member_id:
            member_id = self.get_userinfo()["sub"]
        return f"urn:li:person:{member_id}"

    def _api_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "LinkedIn-Version": self.api_version,
            "X-Restli-Protocol-Version": RESTLI_PROTOCOL,
            "Content-Type": "application/json",
        }

    def ensure_fresh(self) -> None:
        """Refresh when a refresh token exists; fail if the access token is dead."""
        expires = access_token_expiry(self.token)
        if expires is None:
            return
        now = utc_now()
        remaining = (expires - now).total_seconds()
        if remaining <= 0:
            if not self.try_refresh():
                raise LinkedInAuthError(self._expired_message())
            return
        if remaining <= REFRESH_SKEW_SECONDS:
            self.try_refresh()

    def try_refresh(self) -> bool:
        """Exchange a refresh token if LinkedIn granted one. Never raises on failure."""
        if not refresh_token_usable(self.token):
            return False
        if not self.client_id or not self.client_secret:
            print(
                "LinkedIn refresh token is present but LINKEDIN_CLIENT_ID / "
                "LINKEDIN_CLIENT_SECRET are not set, so the access token was not refreshed."
            )
            return False
        response = self._http.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.token["refresh_token"],
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        if response.status_code >= 400:
            print(
                "LinkedIn refresh failed "
                f"({response.status_code}): {response.text[:300]}"
            )
            return False
        self.token = apply_token_payload(self.token, response.json())
        self._persist_token("Refreshed LinkedIn access token.")
        return True

    def _persist_token(self, message: str) -> None:
        if self.token.get("_source") == "env":
            print(
                f"{message} This run will use the new access token. The "
                "LINKEDIN_TOKEN_JSON secret still holds the previous token. "
                "Refresh tokens keep working until they expire; if refresh "
                "stops, re-run linkedin auth and update the secret."
            )
            return
        if self.on_token_update:
            self.on_token_update(self.token)
            print(f"{message} Saved the updated token locally.")

    def get_userinfo(self) -> dict[str, Any]:
        response = self._authorized_get(USERINFO_URL, versioned=False)
        body = response.json()
        subject = body.get("sub")
        if not subject:
            raise RuntimeError(f"LinkedIn userinfo response missing sub: {body}")
        self.token["member_id"] = subject
        if body.get("name"):
            self.token["member_name"] = body["name"]
        return body

    def create_post(
        self,
        commentary: str,
        *,
        image_urn: str | None = None,
        alt_text: str | None = None,
    ) -> str:
        text = commentary.strip()
        if not text:
            raise RuntimeError("Refusing to publish an empty LinkedIn post.")
        escaped = escape_little(text)
        if len(escaped) > COMMENTARY_LIMIT:
            raise RuntimeError(
                f"LinkedIn commentary is {len(escaped)} characters after escaping; "
                f"the Posts API limit is {COMMENTARY_LIMIT}."
            )
        payload: dict[str, Any] = {
            "author": self.author_urn(),
            "commentary": escaped,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if image_urn:
            media: dict[str, str] = {"id": image_urn}
            if alt_text:
                media["altText"] = alt_text[:120]
            payload["content"] = {"media": media}
        response = self._request("POST", POSTS_URL, json_body=payload)
        if response.status_code not in (200, 201):
            raise RuntimeError(
                f"LinkedIn create post failed ({response.status_code}): {response.text[:400]}"
            )
        post_urn = response.headers.get("x-restli-id")
        if not post_urn:
            raise RuntimeError(
                "LinkedIn create post succeeded but x-restli-id was missing. "
                f"Body: {response.text[:300]}"
            )
        return str(post_urn)

    def upload_image(
        self,
        data: bytes,
        *,
        content_type: str = "image/jpeg",
        alt_text: str | None = None,
    ) -> str:
        """Register an image and upload the bytes. Returns ``urn:li:image:...``."""
        del alt_text  # alt text belongs on the post, not the upload
        if not data:
            raise RuntimeError("Image upload received empty bytes.")
        init = self._request(
            "POST",
            IMAGES_INIT_URL,
            json_body={"initializeUploadRequest": {"owner": self.author_urn()}},
        )
        body = init.json()
        value = body.get("value") or {}
        upload_url = value.get("uploadUrl")
        image_urn = value.get("image")
        if not upload_url or not image_urn:
            raise RuntimeError(f"Unexpected LinkedIn image init response: {body}")
        uploaded = self._http.put(
            upload_url,
            content=data,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": content_type,
            },
        )
        if uploaded.status_code == 401:
            raise LinkedInAuthError(self._unauthorized_message(uploaded))
        if uploaded.status_code >= 400:
            raise RuntimeError(
                f"LinkedIn image upload failed ({uploaded.status_code}): {uploaded.text[:300]}"
            )
        return str(image_urn)

    def _authorized_get(self, url: str, *, versioned: bool) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.access_token}"}
        if versioned:
            headers.update(self._api_headers())
        response = self._http.get(url, headers=headers)
        if response.status_code == 401 and self.try_refresh():
            headers["Authorization"] = f"Bearer {self.access_token}"
            response = self._http.get(url, headers=headers)
        if response.status_code == 401:
            raise LinkedInAuthError(self._unauthorized_message(response))
        if response.status_code >= 400:
            raise RuntimeError(
                f"LinkedIn GET {url} failed ({response.status_code}): {response.text[:400]}"
            )
        return response

    def _request(self, method: str, url: str, *, json_body: dict | None = None) -> httpx.Response:
        response = self._send(method, url, json_body)
        if response.status_code == 401 and self.try_refresh():
            response = self._send(method, url, json_body)
        if response.status_code == 401:
            raise LinkedInAuthError(self._unauthorized_message(response))
        if response.status_code >= 400:
            raise RuntimeError(
                f"LinkedIn {method} {url} failed ({response.status_code}): {response.text[:400]}"
            )
        return response

    def _send(self, method: str, url: str, json_body: dict | None) -> httpx.Response:
        kwargs: dict[str, Any] = {"headers": self._api_headers()}
        if json_body is not None:
            kwargs["json"] = json_body
        return self._http.request(method, url, **kwargs)

    def _expired_message(self) -> str:
        expires = self.token.get("expires_at") or "unknown"
        if self.token.get("refresh_token") and not (self.client_id and self.client_secret):
            return (
                f"LinkedIn access token expired at {expires}. A refresh token is "
                "stored, but LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET are "
                "not available to use it."
            )
        if self.token.get("refresh_token"):
            return (
                f"LinkedIn access token expired at {expires} and the refresh "
                "token was rejected. Re-authorize."
            )
        return (
            f"LinkedIn access token expired at {expires}. This token has no "
            "refresh token, which is normal unless LinkedIn granted programmatic "
            "refresh for the app."
        )

    def _unauthorized_message(self, response: httpx.Response) -> str:
        snippet = " ".join(response.text.split())[:300]
        return (
            "LinkedIn returned HTTP 401. The access token is expired or revoked. "
            f"Response: {snippet or '(empty body)'}"
        )


def build_client(
    *,
    api_version: str,
    token: dict[str, Any] | None = None,
    http_client: httpx.Client | None = None,
) -> LinkedInClient:
    loaded = token if token is not None else load_linkedin_token()
    return LinkedInClient(
        loaded,
        api_version=api_version,
        http_client=http_client,
        client_id=get_env("LINKEDIN_CLIENT_ID"),
        client_secret=get_env("LINKEDIN_CLIENT_SECRET"),
        on_token_update=save_linkedin_token,
    )
