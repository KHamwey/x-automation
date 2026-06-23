"""Grok (xAI) text transformation for draft generation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import yaml

from x_automation.config import (
    DEFAULT_XAI_MODEL,
    PROMPTS_PATH,
    XAI_API_BASE,
    get_env,
    load_env,
)


def load_prompts(path: Path | None = None) -> dict[str, Any]:
    prompts_path = path or PROMPTS_PATH
    if not prompts_path.exists():
        raise FileNotFoundError(
            f"Prompts file not found: {prompts_path}. Copy prompts.yaml.example to prompts.yaml."
        )
    with prompts_path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _render_prompt(template: str, variables: dict[str, str]) -> str:
    result = template
    for key, value in variables.items():
        result = result.replace("{{" + key + "}}", value)
    return result


def transform_item(
    *,
    style: str,
    category: str,
    title: str,
    summary: str,
    link: str | None,
    include_url: bool,
    char_budget: int = 260,
    prompts_path: Path | None = None,
) -> str:
    load_env()
    api_key = get_env("XAI_API_KEY")
    if not api_key:
        raise RuntimeError("XAI_API_KEY is required in .env for Grok transforms.")

    prompts = load_prompts(prompts_path)
    styles = prompts.get("styles") or {}
    if style not in styles:
        raise ValueError(f"Unknown grok_style '{style}'. Available: {', '.join(styles)}")

    style_cfg = styles[style]
    system = style_cfg.get("system", "You write concise social posts.")
    user_template = style_cfg.get(
        "user",
        "Write a post about: {{title}}\nSummary: {{summary}}\nMax {{char_budget}} chars.",
    )
    user = _render_prompt(
        user_template,
        {
            "title": title,
            "summary": summary,
            "link": link or "",
            "category": category,
            "char_budget": str(char_budget),
        },
    )

    model = get_env("XAI_MODEL", DEFAULT_XAI_MODEL)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": style_cfg.get("temperature", 0.7),
        "max_tokens": style_cfg.get("max_tokens", 200),
    }

    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{XAI_API_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        body = response.json()

    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError(f"Empty Grok response: {body}")
    text = (choices[0].get("message") or {}).get("content") or ""
    text = text.strip()

    if include_url and link and link not in text:
        suffix = f" {link}"
        if len(text) + len(suffix) <= 280:
            text = text + suffix

    if len(text) > 280:
        text = text[:277] + "..."

    return text
