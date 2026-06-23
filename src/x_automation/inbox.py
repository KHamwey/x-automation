"""Import draft JSON files dropped by Cursor agents."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from x_automation.config import CURSOR_INBOX_DIR, CURSOR_INBOX_PROCESSED_DIR, ensure_data_dir
from x_automation.drafts import add_draft, ensure_tag_prefix


REQUIRED_FIELDS = {"category", "text"}


@dataclass
class ImportResult:
    imported: int = 0
    skipped: int = 0
    errors: int = 0
    error_details: list[str] = field(default_factory=list)


def _validate_payload(payload: dict[str, Any]) -> None:
    missing = REQUIRED_FIELDS - set(payload.keys())
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(sorted(missing))}")
    if not str(payload.get("text", "")).strip():
        raise ValueError("text must not be empty")


def import_inbox(
    *,
    inbox_dir: Path | None = None,
    tag_map: dict[str, str] | None = None,
) -> ImportResult:
    inbox = inbox_dir or CURSOR_INBOX_DIR
    processed_dir = inbox / "processed" if inbox_dir else CURSOR_INBOX_PROCESSED_DIR
    ensure_data_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)

    result = ImportResult()
    files = sorted(inbox.glob("*.json"))
    if not files:
        print(f"No JSON files in {inbox}")
        return result

    for path in files:
        try:
            with path.open(encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                raise ValueError("Inbox file must contain a JSON object")

            _validate_payload(payload)
            category = str(payload["category"])
            text = str(payload["text"]).strip()
            source_url = payload.get("source_url")
            metadata = payload.get("metadata") or {}
            metadata["inbox_file"] = path.name

            tag_emoji = payload.get("tag_emoji") or ""
            if not tag_emoji and tag_map:
                tag_emoji = next(
                    (emoji for emoji, label in tag_map.items() if label == category),
                    "",
                )

            if source_url and source_url not in text:
                text = f"{text} {source_url}".strip()

            if tag_emoji:
                text = ensure_tag_prefix(text, tag_emoji)

            add_draft(
                category=category,
                text=text,
                tag_emoji=tag_emoji or "📌",
                source="cursor",
                source_url=source_url,
                metadata=metadata,
            )
            shutil.move(str(path), str(processed_dir / path.name))
            result.imported += 1
        except Exception as exc:
            result.errors += 1
            result.error_details.append(f"{path.name}: {exc}")

    return result
