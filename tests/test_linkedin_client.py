"""LinkedIn API client with mocked HTTP."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from x_automation.linkedin.auth_flow import (
    build_authorization_url,
    exchange_authorization_code,
    parse_callback_url,
)
from x_automation.linkedin.client import (
    IMAGES_INIT_URL,
    POSTS_URL,
    TOKEN_URL,
    USERINFO_URL,
    LinkedInClient,
)
from x_automation.linkedin.errors import LinkedInAuthError
from x_automation.linkedin.tokens import apply_token_payload


def _client(handler, token=None, **kwargs) -> LinkedInClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    payload = token or {
        "access_token": "token",
        "member_id": "abc",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    return LinkedInClient(
        payload,
        api_version="202611",
        http_client=http,
        client_id=kwargs.get("client_id"),
        client_secret=kwargs.get("client_secret"),
    )


def test_authorization_url_uses_member_scopes():
    url = build_authorization_url(
        client_id="cid",
        redirect_uri="http://127.0.0.1:8765/callback",
        state="state123",
    )
    assert url.startswith("https://www.linkedin.com/oauth/v2/authorization?")
    assert "response_type=code" in url
    assert "client_id=cid" in url
    assert "scope=openid%20profile%20w_member_social" in url
    assert "state=state123" in url


def test_exchange_code_posts_form_and_records_expiry():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == TOKEN_URL
        body = request.content.decode()
        assert "grant_type=authorization_code" in body
        assert "code=abc" in body
        assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Fcallback" in body
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "expires_in": 5184000,
                "scope": "openid profile w_member_social",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        token = exchange_authorization_code(
            code="abc",
            redirect_uri="http://127.0.0.1:8765/callback",
            client_id="cid",
            client_secret="secret",
            http_client=http,
        )
    assert token["access_token"] == "access"
    assert "refresh_token" not in token
    assert token["expires_at"].startswith("20")
    expiry = datetime.fromisoformat(token["expires_at"])
    assert expiry > datetime.now(timezone.utc)


def test_parse_callback_requires_code():
    code, state = parse_callback_url("http://127.0.0.1:8765/callback?code=abc&state=xyz")
    assert code == "abc"
    assert state == "xyz"
    with pytest.raises(RuntimeError):
        parse_callback_url("http://127.0.0.1:8765/callback")


def test_userinfo_reads_sub_without_version_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == USERINFO_URL
        assert request.headers["Authorization"] == "Bearer token"
        assert "LinkedIn-Version" not in request.headers
        return httpx.Response(200, json={"sub": "person123", "name": "Ada Lovelace"})

    client = _client(handler, token={"access_token": "token"})
    profile = client.get_userinfo()
    assert profile["sub"] == "person123"
    assert client.author_urn() == "urn:li:person:person123"


def test_create_post_headers_author_and_escaped_commentary():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == POSTS_URL
        assert request.headers["Authorization"] == "Bearer token"
        assert request.headers["LinkedIn-Version"] == "202611"
        assert request.headers["X-Restli-Protocol-Version"] == "2.0.0"
        body = json.loads(request.content)
        assert body["author"] == "urn:li:person:abc"
        assert body["visibility"] == "PUBLIC"
        assert body["lifecycleState"] == "PUBLISHED"
        assert body["distribution"]["feedDistribution"] == "MAIN_FEED"
        assert body["isReshareDisabledByAuthor"] is False
        assert r"\(approx\)" in body["commentary"]
        assert "#FedRAMP" in body["commentary"]
        assert r"\#FedRAMP" not in body["commentary"]
        return httpx.Response(201, headers={"x-restli-id": "urn:li:share:99"})

    client = _client(handler)
    urn = client.create_post("Cost (approx) is the story.\n\n#FedRAMP")
    assert urn == "urn:li:share:99"


def test_image_upload_then_post_uses_image_urn():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == IMAGES_INIT_URL:
            body = json.loads(request.content)
            assert body["initializeUploadRequest"]["owner"] == "urn:li:person:abc"
            assert request.headers["LinkedIn-Version"] == "202611"
            assert request.headers["X-Restli-Protocol-Version"] == "2.0.0"
            return httpx.Response(
                200,
                json={
                    "value": {
                        "uploadUrl": "https://uploads.example/img",
                        "image": "urn:li:image:xyz",
                    }
                },
            )
        if url == "https://uploads.example/img":
            assert request.method == "PUT"
            assert request.content == b"jpg-bytes"
            assert request.headers["Content-Type"] == "image/jpeg"
            return httpx.Response(201)
        if url == POSTS_URL:
            body = json.loads(request.content)
            assert body["content"]["media"]["id"] == "urn:li:image:xyz"
            assert body["content"]["media"]["altText"] == "rocket"
            return httpx.Response(201, headers={"x-restli-id": "urn:li:share:7"})
        raise AssertionError(url)

    client = _client(handler)
    image_urn = client.upload_image(b"jpg-bytes", content_type="image/jpeg")
    assert image_urn == "urn:li:image:xyz"
    assert client.create_post("Hello", image_urn=image_urn, alt_text="rocket") == "urn:li:share:7"


def test_expired_token_without_refresh_fails_before_http():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"should not call LinkedIn: {request.url}")

    client = _client(
        handler,
        token={
            "access_token": "old",
            "expires_at": "2020-01-01T00:00:00+00:00",
            "member_id": "abc",
        },
    )
    with pytest.raises(LinkedInAuthError) as caught:
        client.ensure_fresh()
    assert "LINKEDIN_TOKEN_JSON" in caught.value.banner()
    assert "linkedin auth" in caught.value.banner()


def test_expired_token_refreshes_then_posts():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == TOKEN_URL:
            body = request.content.decode()
            assert "grant_type=refresh_token" in body
            assert "refresh_token=refresh-me" in body
            assert "client_id=cid" in body
            return httpx.Response(
                200,
                json={"access_token": "new-access", "expires_in": 5184000},
            )
        if str(request.url) == POSTS_URL:
            assert request.headers["Authorization"] == "Bearer new-access"
            return httpx.Response(201, headers={"x-restli-id": "urn:li:share:1"})
        raise AssertionError(request.url)

    client = _client(
        handler,
        token={
            "access_token": "old",
            "refresh_token": "refresh-me",
            "expires_at": "2020-01-01T00:00:00+00:00",
            "member_id": "abc",
            "_source": "env",
        },
        client_id="cid",
        client_secret="secret",
    )
    client.ensure_fresh()
    assert client.create_post("Hello") == "urn:li:share:1"
    assert calls[0] == TOKEN_URL
    assert "refresh_token" in client.token


def test_401_without_refresh_is_an_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Invalid access token"})

    client = _client(handler)
    with pytest.raises(LinkedInAuthError) as caught:
        client.get_userinfo()
    assert "401" in str(caught.value)


def test_401_retries_once_after_refresh():
    state = {"hits": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 1000})
        state["hits"] += 1
        if state["hits"] == 1:
            return httpx.Response(401, text="expired")
        assert request.headers["Authorization"] == "Bearer fresh"
        return httpx.Response(200, json={"sub": "abc", "name": "Ada"})

    client = _client(
        handler,
        token={
            "access_token": "old",
            "refresh_token": "refresh-me",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "member_id": "abc",
            "_source": "env",
        },
        client_id="cid",
        client_secret="secret",
    )
    profile = client.get_userinfo()
    assert profile["sub"] == "abc"
    assert state["hits"] == 2


def test_apply_token_payload_keeps_existing_refresh_token():
    updated = apply_token_payload(
        {"access_token": "old", "refresh_token": "keep", "_source": "file"},
        {"access_token": "new", "expires_in": 60},
        obtained_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert updated["access_token"] == "new"
    assert updated["refresh_token"] == "keep"
    assert updated["expires_at"] == "2026-01-01T00:01:00+00:00"
    assert updated["_source"] == "file"
