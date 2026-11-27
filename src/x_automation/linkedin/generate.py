"""Turn a feed item into a LinkedIn post with Grok.

The model writes the draft. This module then makes sure the post has a
question or invitation, a source link when requested, and a few hashtags,
and that it stays inside the character budget. LinkedIn ``little`` escaping
happens later, at publish time, so dry-run output stays readable.
"""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path
from typing import Any

import httpx

from x_automation.config import DEFAULT_XAI_MODEL, XAI_API_BASE, get_env, load_env
from x_automation.linkedin.config import load_linkedin_prompts

_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_HASHTAG_TOKEN = re.compile(r"^#[A-Za-z][A-Za-z0-9]*$")
_ENGAGEMENT_CUES = (
    "weigh in",
    "share your",
    "let me know",
    "comment if",
    "curious how",
    "what do you think",
    "i'd like to hear",
    "would love to hear",
)


def plain_summary(summary: str, limit: int = 1500) -> str:
    text = unescape(_TAG.sub(" ", summary or ""))
    text = _WHITESPACE.sub(" ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    text = _WHITESPACE.sub(" ", text)
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def clean_model_text(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
        cleaned = cleaned.strip()
    cleaned = re.sub(
        r"^(here is|here's) (the )?(post|linkedin post)[:\s-]*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'":
        inner = cleaned[1:-1].strip()
        if inner:
            cleaned = inner
    cleaned = cleaned.replace("**", "")
    cleaned = _BLANK_LINES.sub("\n\n", cleaned)
    return cleaned.strip()


def _is_hashtag_line(line: str) -> bool:
    tokens = line.split()
    return bool(tokens) and all(_HASHTAG_TOKEN.match(token) for token in tokens)


def split_trailing_hashtags(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    trailing: list[str] = []
    while lines and _is_hashtag_line(lines[-1]):
        trailing.append(lines.pop())
        while lines and not lines[-1].strip():
            lines.pop()
    trailing.reverse()
    body = "\n".join(lines).strip()
    tags = " ".join(" ".join(line.split()) for line in trailing).strip()
    return body, tags


def has_engagement(text: str) -> bool:
    if "?" in text:
        return True
    lower = text.lower()
    return any(cue in lower for cue in _ENGAGEMENT_CUES)


def _normalize_hashtag(tag: str) -> str:
    token = tag.strip()
    if not token:
        return ""
    if not token.startswith("#"):
        token = "#" + token
    return token


def compose_post(
    *,
    body: str,
    link: str | None,
    include_url: bool,
    hashtags: list[str],
    char_budget: int,
) -> str:
    main, tags = split_trailing_hashtags(clean_model_text(body))
    if include_url and link and link not in main:
        main = main.rstrip() + "\n\n" + link
    if not has_engagement(main):
        main = main.rstrip() + "\n\nWhat stands out to you?"
    if not tags and not re.search(r"(^|\s)#[A-Za-z]", main):
        chosen = [_normalize_hashtag(tag) for tag in hashtags[:4]]
        tags = " ".join(tag for tag in chosen if tag)
    post = main.rstrip()
    if tags:
        post = f"{post}\n\n{tags}"
    return trim_to_budget(post.strip(), char_budget)


def trim_to_budget(post: str, budget: int) -> str:
    if budget < 1 or len(post) <= budget:
        return post
    main, tags = split_trailing_hashtags(post)
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", main) if part.strip()]
    question = ""
    if len(paragraphs) > 1 and "?" in paragraphs[-1]:
        question = paragraphs.pop()
    link = ""
    if paragraphs and paragraphs[-1].startswith("http"):
        link = paragraphs.pop()
    tail_bits = [bit for bit in (link, question, tags) if bit]
    reserved = sum(len(bit) + 2 for bit in tail_bits)
    room = budget - reserved
    body = "\n\n".join(paragraphs).strip()
    if room < 40:
        clipped = post[: budget - 1].rstrip()
        return clipped + "…"
    if len(body) > room:
        body = body[: room - 1].rstrip() + "…"
    parts = [body, *tail_bits]
    return "\n\n".join(part for part in parts if part).strip()


def _render(template: str, variables: dict[str, str]) -> str:
    rendered = template
    for key, value in variables.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def generate_linkedin_post(
    *,
    industry_name: str,
    angle: str,
    style: str,
    title: str,
    summary: str,
    link: str | None,
    include_url: bool,
    hashtags: list[str],
    char_budget: int,
    prompts_path: Path | None = None,
    http_client: httpx.Client | None = None,
) -> str:
    load_env()
    api_key = get_env("XAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "XAI_API_KEY is required to write LinkedIn posts. "
            "Put it in .env locally, or set the XAI_API_KEY GitHub secret."
        )

    prompts = load_linkedin_prompts(prompts_path)
    styles: dict[str, Any] = prompts.get("styles") or {}
    if style not in styles:
        known = ", ".join(styles) or "(none)"
        raise ValueError(f"Unknown grok_style '{style}'. Available: {known}")
    style_cfg = styles[style]
    system = style_cfg.get("system") or "You write professional LinkedIn posts."
    user_template = style_cfg.get("user") or (
        "Industry: {{industry}}\nHeadline: {{title}}\nSummary: {{summary}}\n"
        "Link: {{link}}\nHashtags: {{hashtags}}\nBudget: {{char_budget}}"
    )
    variables = {
        "industry": industry_name,
        "angle": angle,
        "title": title,
        "summary": plain_summary(summary),
        "link": link or "",
        "hashtags": " ".join(hashtags),
        "char_budget": str(char_budget),
    }
    model = get_env("XAI_MODEL", DEFAULT_XAI_MODEL)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _render(system, variables)},
            {"role": "user", "content": _render(user_template, variables)},
        ],
        "temperature": style_cfg.get("temperature", 0.6),
        "max_tokens": style_cfg.get("max_tokens", 800),
    }
    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=60.0)
    try:
        response = client.post(
            f"{XAI_API_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    finally:
        if owns_client:
            client.close()
    if response.status_code == 401:
        raise RuntimeError("xAI rejected XAI_API_KEY (HTTP 401).")
    response.raise_for_status()
    body = response.json()
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError(f"Empty Grok response: {body}")
    content = ((choices[0].get("message") or {}).get("content") or "").strip()
    if not content:
        raise RuntimeError("Grok returned an empty LinkedIn post.")
    return compose_post(
        body=content,
        link=link,
        include_url=include_url,
        hashtags=hashtags,
        char_budget=char_budget,
    )
