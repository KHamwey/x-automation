"""Grok (xAI) text transformation for draft generation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from x_automation.mentions import MentionCandidate

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


@dataclass(frozen=True)
class Annotations:
    hashtags: list[str]
    mentions: list[MentionCandidate]


def _json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def parse_annotations(text: str) -> Annotations:
    """Read hashtag and mention subjects from a Grok JSON reply.

    A ``handle`` field is ignored. Handles are resolved later with the X API.
    """
    data = _json_object(text)
    hashtags: list[str] = []
    raw_tags = data.get("hashtags")
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]
    for raw in raw_tags or []:
        if isinstance(raw, str) and raw.strip():
            hashtags.append(raw.strip())

    mentions: list[MentionCandidate] = []
    raw_mentions = data.get("mentions")
    if isinstance(raw_mentions, dict):
        raw_mentions = [raw_mentions]
    for raw in raw_mentions or []:
        if isinstance(raw, str):
            name, kind = raw.strip(), ""
        elif isinstance(raw, dict):
            name = str(raw.get("name") or "").strip()
            kind = str(raw.get("kind") or raw.get("type") or "").strip()
        else:
            continue
        if not name:
            continue
        mentions.append(MentionCandidate(name=name, kind=kind))
        if len(mentions) >= 4:
            break
    return Annotations(hashtags=hashtags[:4], mentions=mentions)


def suggest_annotations(
    *,
    category: str,
    title: str,
    summary: str,
    prompts_path: Path | None = None,
    prompts: dict[str, Any] | None = None,
) -> Annotations:
    """Ask Grok for story hashtags and mention subjects.

    Returns empty lists when ``annotations`` is missing from prompts.yaml or
    the reply is not usable. Callers then keep the static-tag fallback and
    tag nobody.
    """
    loaded = prompts if prompts is not None else load_prompts(prompts_path)
    block = loaded.get("annotations")
    if not isinstance(block, dict):
        return Annotations(hashtags=[], mentions=[])
    system_template = str(block.get("system") or "").strip()
    user_template = str(block.get("user") or "").strip()
    if not system_template or not user_template:
        return Annotations(hashtags=[], mentions=[])

    load_env()
    api_key = get_env("XAI_API_KEY")
    if not api_key:
        return Annotations(hashtags=[], mentions=[])

    variables = {
        "title": title,
        "summary": summary,
        "category": category,
    }
    model = get_env("XAI_MODEL", DEFAULT_XAI_MODEL)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _render_prompt(system_template, variables).strip()},
            {"role": "user", "content": _render_prompt(user_template, variables).strip()},
        ],
        "temperature": float(block.get("temperature", 0.2)),
        "max_tokens": int(block.get("max_tokens", 250)),
    }

    try:
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
    except Exception:
        return Annotations(hashtags=[], mentions=[])

    choices = body.get("choices") or []
    if not choices:
        return Annotations(hashtags=[], mentions=[])
    content = (choices[0].get("message") or {}).get("content") or ""
    return parse_annotations(str(content))
