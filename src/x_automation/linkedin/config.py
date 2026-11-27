"""LinkedIn feeds, prompts, and runtime paths.

The tracked ``linkedin_feeds.yaml`` is what GitHub Actions reads. The
``.example`` file is the same starter. A missing real file falls back to
the example so a fresh checkout still runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from x_automation.config import DATA_DIR, PROJECT_ROOT, get_env

LINKEDIN_FEEDS_PATH = PROJECT_ROOT / "linkedin_feeds.yaml"
LINKEDIN_FEEDS_EXAMPLE_PATH = PROJECT_ROOT / "linkedin_feeds.yaml.example"
LINKEDIN_PROMPTS_PATH = PROJECT_ROOT / "linkedin_prompts.yaml"
LINKEDIN_PROMPTS_EXAMPLE_PATH = PROJECT_ROOT / "linkedin_prompts.yaml.example"
LINKEDIN_TOKEN_PATH = DATA_DIR / "linkedin_token.json"
LINKEDIN_STATE_PATH = PROJECT_ROOT / "state" / "linkedin_posted.json"

# Latest Marketing API version documented for November 2026.
# https://learn.microsoft.com/en-us/linkedin/marketing/versioning
DEFAULT_LINKEDIN_API_VERSION = "202611"
DEFAULT_DAILY_CAP = 2
DEFAULT_MIN_SPACING_HOURS = 3
DEFAULT_MAX_POSTS_PER_RUN = 1
DEFAULT_CHAR_BUDGET = 1600
DEFAULT_DAY_TIMEZONE = "America/New_York"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/callback"

LINKEDIN_SCOPES = ("openid", "profile", "w_member_social")


def resolve_config_file(preferred: Path, example: Path, label: str) -> Path:
    if preferred.exists():
        return preferred
    if example.exists():
        return example
    raise FileNotFoundError(
        f"LinkedIn {label} not found. Expected {preferred} or {example}."
    )


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a YAML mapping.")
    return data


def load_linkedin_config(path: Path | None = None) -> dict[str, Any]:
    feeds_path = path or resolve_config_file(
        LINKEDIN_FEEDS_PATH, LINKEDIN_FEEDS_EXAMPLE_PATH, "feeds config"
    )
    return load_yaml(feeds_path)


def load_linkedin_prompts(path: Path | None = None) -> dict[str, Any]:
    prompts_path = path or resolve_config_file(
        LINKEDIN_PROMPTS_PATH, LINKEDIN_PROMPTS_EXAMPLE_PATH, "prompts config"
    )
    return load_yaml(prompts_path)


def defaults_of(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("defaults") or {}
    if not isinstance(raw, dict):
        raise ValueError("linkedin feeds defaults must be a mapping.")
    return raw


def api_version(config: dict[str, Any] | None = None) -> str:
    env_version = get_env("LINKEDIN_API_VERSION")
    if env_version and env_version.strip():
        return env_version.strip()
    if config:
        configured = defaults_of(config).get("api_version")
        if configured:
            return str(configured)
    return DEFAULT_LINKEDIN_API_VERSION


def enabled_industries(config: dict[str, Any]) -> list[dict[str, Any]]:
    block = config.get("industries")
    if block is None:
        block = config.get("topics") or {}
    if not isinstance(block, dict):
        raise ValueError("linkedin industries must be a mapping of id -> settings.")

    industries: list[dict[str, Any]] = []
    for industry_id, cfg in block.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"Industry {industry_id} must be a mapping.")
        if not cfg.get("enabled", True):
            continue
        feeds = []
        for entry in cfg.get("feeds") or []:
            if isinstance(entry, str):
                feeds.append({"url": entry, "include_url": True})
                continue
            if not isinstance(entry, dict) or not entry.get("url"):
                continue
            feeds.append(
                {
                    "url": str(entry["url"]),
                    "include_url": bool(entry.get("include_url", True)),
                }
            )
        if not feeds:
            raise ValueError(f"Industry {industry_id} has no feeds.")
        style = cfg.get("grok_style")
        if not style:
            raise ValueError(f"Industry {industry_id} is missing grok_style.")
        weight = int(cfg.get("weight") or 1)
        if weight < 1:
            raise ValueError(f"Industry {industry_id} weight must be >= 1.")
        hashtags = [str(tag) for tag in (cfg.get("hashtags") or [])]
        industries.append(
            {
                "id": str(industry_id),
                "name": str(cfg.get("name") or industry_id),
                "weight": weight,
                "grok_style": str(style),
                "hashtags": hashtags,
                "angle": str(cfg.get("angle") or "").strip(),
                "feeds": feeds,
            }
        )
    if not industries:
        raise ValueError("No enabled LinkedIn industries. Edit linkedin_feeds.yaml.")
    return industries
