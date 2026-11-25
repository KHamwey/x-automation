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

Manual posting waits until you approve. `drafts autopost` skips that and posts on its own. Max **3 posts per category per UTC day**. Fetch rotates across categories so the first feed does not starve the rest. After `drafts analytics`, fetch also scales each category's draft budget by engagement.

```bash
cp feeds.yaml.example feeds.yaml
cp prompts.yaml.example prompts.yaml
cp tags.json.example data/tags.json
# .env: XAI_API_KEY=... (optional; use --no-grok without xAI)

python -m x_automation.cli drafts fetch              # RSS → Grok → pending
python -m x_automation.cli drafts fetch --no-grok    # RSS only, no xAI
python -m x_automation.cli drafts fetch --no-preferences  # ignore engagement weights

python -m x_automation.cli drafts import-inbox       # from data/inbox/cursor/

python -m x_automation.cli drafts list --status pending
python -m x_automation.cli drafts approve <id>
python -m x_automation.cli drafts reject <id>

python -m x_automation.cli drafts publish            # dry-run
python -m x_automation.cli drafts publish --execute

python -m x_automation.cli drafts autopost --dry-run # fetch + approve, no post
python -m x_automation.cli drafts autopost           # fetch, approve, post

python -m x_automation.cli drafts stats
python -m x_automation.cli drafts analytics          # score posts, write weights
```

Unattended posts run from cron at 8:00, 13:00, and 18:00 America/New_York:

```bash
chmod +x scripts/autopost.sh
# crontab lines:
# CRON_TZ=America/New_York
# 0 8,13,18 * * * /home/wadeflavor/projects/x-automation/scripts/autopost.sh
```

Output is appended to `data/autopost.log`. News headlines become dry, sarcastic lines in a right-leaning voice (`voice: sarcastic_conservative` in `feeds.yaml`); tragic stories are dropped when Grok replies `SKIP`. Comedy uses `sarcastic_comedy` (the same slant when the source is politics or culture). Motocross stays on the race with `sarcastic_sports`. Voice text lives under `voices:` in `prompts.yaml`. Links are left off so each post stays on the text-only X API rate. If an older local `prompts.yaml` has no `voices:` section, copy `prompts.yaml.example` over it.

Hashtags and @mentions are optional and come from local config (these files are gitignored, so copy the keys from the examples):

- `topic_hashtags`, `keep_static_hashtags` (0 or 1), `max_topic_hashtags`, and `max_hashtags` on each category in `feeds.yaml`. The draft step asks Grok for one or two story tags (`#FBI`, `#SNL`, `#Supercross`) and can keep the first static tag in `hashtags`. A post ends up with at most 2–3 tags. Without those keys, every static tag is still appended.
- `mentions` and `max_mentions` on each category. The main public figure or organization is @mentioned only after `GET /2/users/search` finds one verified account whose display name matches. Private people, victims, and minors are not tagged. If lookup fails or the match is ambiguous, the post tags nobody. Mentions are placed after the text so the post does not open with `@`.
- `annotations` in `prompts.yaml` (the JSON instructions for that draft step). If the key is missing, no story tags or mention subjects are requested.

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
