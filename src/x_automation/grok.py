"""Grok (xAI) text transformation for draft generation."""

from __future__ import annotations

from dataclasses import dataclass
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

# Used only when prompts.yaml does not name a voice for the category.
DEFAULT_VOICE_NAME = "sarcastic_conservative"
_CATEGORY_VOICE_FALLBACK = {
    "comedy": "sarcastic_comedy",
    "motocross": "sarcastic_sports",
}


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


def category_voice_name(category_cfg: dict[str, Any] | None) -> str | None:
    """Return a category's configured voice name.

    ``voice`` wins. ``tone`` is an alias so either feeds.yaml field works.
    Blank values are ignored.
    """
    if not category_cfg:
        return None
    for key in ("voice", "tone"):
        if key not in category_cfg or category_cfg[key] is None:
            continue
        text = str(category_cfg[key]).strip()
        if text:
            return text
    return None


def resolve_voice_name(
    prompts: dict[str, Any],
    voice: str | None = None,
    category: str | None = None,
) -> str:
    """Pick a voice name: category field, then prompts defaults, then fallback."""
    explicit = (voice or "").strip()
    if explicit:
        return explicit

    by_category = prompts.get("default_voices") or {}
    if isinstance(by_category, dict) and category:
        named = str(by_category.get(category) or "").strip()
        if named:
            return named

    if category and category in _CATEGORY_VOICE_FALLBACK:
        return _CATEGORY_VOICE_FALLBACK[category]

    default = str(prompts.get("default_voice") or "").strip()
    if default:
        return default
    return DEFAULT_VOICE_NAME


def voice_instructions(prompts: dict[str, Any], name: str) -> str:
    voices = prompts.get("voices") or {}
    if not isinstance(voices, dict) or name not in voices:
        available = ", ".join(str(key) for key in voices) if isinstance(voices, dict) else "(none)"
        raise ValueError(
            f"Unknown voice '{name}'. Available: {available}. "
            "Define it under voices: in prompts.yaml (see prompts.yaml.example)."
        )
    text = str(voices[name] or "").strip()
    if not text:
        raise ValueError(f"Voice '{name}' has no instructions.")
    return text


@dataclass(frozen=True)
class TransformRequest:
    voice: str
    system: str
    user: str
    temperature: float
    max_tokens: int


def build_transform_request(
    *,
    style: str,
    category: str,
    title: str,
    summary: str,
    link: str | None,
    char_budget: int = 260,
    voice: str | None = None,
    prompts_path: Path | None = None,
    prompts: dict[str, Any] | None = None,
) -> TransformRequest:
    """Assemble the Grok messages for one item. Does not call the API."""
    loaded = prompts if prompts is not None else load_prompts(prompts_path)
    styles = loaded.get("styles") or {}
    if style not in styles:
        raise ValueError(f"Unknown grok_style '{style}'. Available: {', '.join(styles)}")

    style_cfg = styles[style]
    voice_name = resolve_voice_name(loaded, voice, category)
    instructions = voice_instructions(loaded, voice_name)
    system_template = style_cfg.get("system") or "You write concise social posts."
    if "{{voice}}" not in system_template:
        system_template = system_template.rstrip() + "\n\n{{voice}}"
    user_template = style_cfg.get(
        "user",
        "Write a post about: {{title}}\nSummary: {{summary}}\nMax {{char_budget}} chars.",
    )
    variables = {
        "title": title,
        "summary": summary,
        "link": link or "",
        "category": category,
        "char_budget": str(char_budget),
        "voice": instructions,
        "voice_name": voice_name,
    }
    return TransformRequest(
        voice=voice_name,
        system=_render_prompt(system_template, variables).strip(),
        user=_render_prompt(user_template, variables).strip(),
        temperature=float(style_cfg.get("temperature", 0.7)),
        max_tokens=int(style_cfg.get("max_tokens", 200)),
    )


def transform_item(
    *,
    style: str,
    category: str,
    title: str,
    summary: str,
    link: str | None,
    include_url: bool,
    char_budget: int = 260,
    voice: str | None = None,
    prompts_path: Path | None = None,
) -> str:
    load_env()
    api_key = get_env("XAI_API_KEY")
    if not api_key:
        raise RuntimeError("XAI_API_KEY is required in .env for Grok transforms.")

    request = build_transform_request(
        style=style,
        category=category,
        title=title,
        summary=summary,
        link=link,
        char_budget=char_budget,
        voice=voice,
        prompts_path=prompts_path,
    )

    model = get_env("XAI_MODEL", DEFAULT_XAI_MODEL)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": request.system},
            {"role": "user", "content": request.user},
        ],
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
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
    text = text.strip().strip("\"'")

    if text.upper().rstrip(".") == "SKIP":
        return "SKIP"

    if include_url and link and link not in text:
        suffix = f" {link}"
        if len(text) + len(suffix) <= 280:
            text = text + suffix

    if len(text) > 280:
        text = text[:277] + "..."

    return text
