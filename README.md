# x-automation

CLI tools for your X account: bulk delete, emoji-targeted delete, and approve-first posting from RSS/Grok/Cursor drafts.

Requires [X Developer Console](https://console.x.com) credits + OAuth app. X Premium does not include API access.

## Setup

```bash
cd x-automation
python3 -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip && python -m pip install .

cp .env.example .env          # CLIENT_ID, CLIENT_SECRET, REDIRECT_URI
python -m x_automation.cli auth
```

Run all commands from this directory.

## Delete

Dry-run by default. Pass `--execute` to actually delete.

```bash
python -m x_automation.cli delete-all                    # preview all
python -m x_automation.cli delete-all --execute          # type DELETE ALL
python -m x_automation.cli delete-all --execute --resume # after interruption

cp tags.json.example data/tags.json
python -m x_automation.cli delete --emoji "🏗" --execute
python -m x_automation.cli delete --tag manufacturing --execute
```

Posts must contain the emoji in their text to match. Progress saved to `data/checkpoint.json`.

## Drafts (RSS + Grok + Cursor)

Nothing posts until you approve and publish. Max **3 posts per category per UTC day**.

```bash
cp feeds.yaml.example feeds.yaml
cp prompts.yaml.example prompts.yaml
cp tags.json.example data/tags.json
# .env: XAI_API_KEY=... (optional; use --no-grok without xAI)

python -m x_automation.cli drafts fetch              # RSS → Grok → pending
python -m x_automation.cli drafts fetch --no-grok    # RSS only, no xAI

python -m x_automation.cli drafts import-inbox       # from data/inbox/cursor/

python -m x_automation.cli drafts list --status pending
python -m x_automation.cli drafts approve <id>
python -m x_automation.cli drafts reject <id>

python -m x_automation.cli drafts publish            # dry-run
python -m x_automation.cli drafts publish --execute

python -m x_automation.cli drafts stats
```

Cursor agents: drop JSON in `data/inbox/cursor/` → `import-inbox`. See [docs/CURSOR_AGENTS.md](docs/CURSOR_AGENTS.md).

## Costs (approx)

| Action | Cost |
|--------|------|
| List your posts (owned read) | ~$0.001/post |
| Delete post | ~$0.005–0.010 each |
| Post text-only | ~$0.015 |
| Post with URL | ~$0.20 |
| Grok rewrite | xAI billing (separate) |

## Notes

- `--execute` required for real deletes and posts
- Deletions are irreversible — review `data/inventory-*.json` first
- Rate limits enforced automatically ([docs](https://docs.x.com/x-api/fundamentals/rate-limits))
- Local API testing: [X API Playground](https://github.com/xdevplatform/playground) + `--api-base-url http://localhost:8080`
