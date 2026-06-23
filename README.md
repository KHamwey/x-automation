# x-automation

Programmatic tools for managing your X account — starting with safe bulk deletion of posts and replies.

<!--
  Architecture: CLI (cli.py) → delete orchestration (delete.py) → timeline fetch
  (timeline.py) + rate limiting (rate_limit.py). Auth is OAuth2 PKCE (auth.py);
  config (config.py) loads .env and data/token.json. Emoji/tag filtering
  (filters.py) is client-side substring match after fetch. Default is dry-run;
  --execute requires typed confirmation. See module docstrings in src/x_automation/
  for API endpoints, billing, and safety behavior before spending credits.
-->

## Prerequisites

- Python 3.9+
- An [X Developer Console](https://console.x.com) app with OAuth 2.0 credentials
- API credits loaded in the Developer Console (pay-per-use; see cost notes below)
- Scopes: `tweet.read`, `tweet.write`, `users.read`, `offline.access`

## Setup

```bash
cd x-automation
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .

cp .env.example .env
# Edit .env with real CLIENT_ID and CLIENT_SECRET from Developer Console
```

Run commands from the `x-automation` directory so `.env` and `data/` resolve correctly.

In the Developer Console, set your OAuth 2.0 redirect URI to match `.env` (e.g. `http://127.0.0.1:8080/callback`).

### Authenticate (one time)

```bash
python -m x_automation.cli auth
```

This opens a browser, authorizes your app, and saves tokens to `data/token.json` (gitignored).

## Usage

### Delete all posts, replies, and reposts (dry-run first)

```bash
# List everything that would be deleted (no API writes, only reads)
python -m x_automation.cli delete-all

# Actually delete (requires typing DELETE ALL)
python -m x_automation.cli delete-all --execute

# Resume after interruption
python -m x_automation.cli delete-all --execute --resume
```

### Delete posts matching an emoji tag

Tag posts with a distinctive emoji when you create them (e.g. `🟦`). Copy the example tag map:

```bash
cp tags.json.example data/tags.json
```

```bash
# By emoji
python -m x_automation.cli delete --emoji "🟦" --execute

# By tag alias from tags.json
python -m x_automation.cli delete --tag portfolio --execute
```

### Flags

| Flag | Description |
|------|-------------|
| `--execute` | Perform deletions (default is dry-run) |
| `--api-base-url URL` | Override API host (see Playground below) |
| `--resume` | Continue from `data/checkpoint.json` |

## Draft pipeline (approve-first posting)

RSS feeds and Cursor agents create **drafts** locally. Nothing posts to X until you approve and publish.

### Setup

```bash
cp feeds.yaml.example feeds.yaml      # edit RSS URLs and categories
cp prompts.yaml.example prompts.yaml  # Grok prompt templates
cp tags.json.example data/tags.json

# Add to .env:
# XAI_API_KEY=...   (for Grok transforms in drafts fetch)
# DRAFTS_DAILY_CAP=3
```

### Workflow

```bash
# 1. Fetch RSS → Grok → pending drafts (xAI cost only, no X post cost)
python -m x_automation.cli drafts fetch

# 2. Or import Cursor agent JSON from data/inbox/cursor/
python -m x_automation.cli drafts import-inbox

# 3. Review and approve
python -m x_automation.cli drafts list --status pending
python -m x_automation.cli drafts show <draft-id>
python -m x_automation.cli drafts approve <draft-id>

# 4. Publish (dry-run first)
python -m x_automation.cli drafts publish
python -m x_automation.cli drafts publish --execute

# 5. Check daily caps (3 per category per UTC day)
python -m x_automation.cli drafts stats
```

See [docs/CURSOR_AGENTS.md](docs/CURSOR_AGENTS.md) for Cursor inbox format.

### Posting costs

| Post type | Rough X API cost |
|-----------|------------------|
| Text-only | ~$0.015 |
| Contains URL | ~$0.20 |

Drafts show estimated cost before publish. Grok/xAI usage is billed separately via xAI.

### Cron example

```bash
# Fetch new drafts every 2 hours (no X API cost)
0 */2 * * * cd ~/Projects/x-automation && .venv/bin/python -m x_automation.cli drafts fetch
```

Review and publish manually after approving drafts.

## Local testing with X API Playground (free)

The [X API Playground](https://github.com/xdevplatform/playground) simulates API v2 locally — no credits consumed, but **does not touch your real account**.

```bash
# Install playground (requires Go)
go install github.com/xdevplatform/playground/cmd/playground@latest
playground start
```

In another terminal:

```bash
python -m x_automation.cli delete-all --api-base-url http://localhost:8080
python -m x_automation.cli delete-all --execute --api-base-url http://localhost:8080
```

For playground, set `BEARER_TOKEN=test` in `.env` instead of running `auth` (or use the saved token flow with playground's test bearer).

## Rate limits

The tool respects [X API rate limits](https://docs.x.com/x-api/fundamentals/rate-limits):

| Operation | Limit | Tool behavior |
|-----------|-------|---------------|
| List posts | 900 / 15 min | Header tracking + pacing |
| Delete post | 50 / 15 min | ~20s between deletes (45/15min safety margin) |
| 429 responses | — | Sleep until `x-rate-limit-reset` |

Large histories take hours to delete fully. Progress is checkpointed to `data/checkpoint.json`.

## Cost estimates

Rate limits and billing are separate. Typical one-time cleanup costs (owned reads + deletes):

| Posts | Est. cost | Min. delete time |
|-------|-----------|------------------|
| 100 | ~$0.60–1.10 | ~30 min |
| 1,000 | ~$6–11 | ~5 hours |
| 5,000 | ~$30–55 | ~25 hours |

Confirm current rates in your Developer Console. X Premium does **not** include API access.

## Safety

- **Dry-run is the default** — always review the inventory JSON in `data/` before `--execute`
- Deletions are **irreversible**
- `data/` (tokens, checkpoints, inventories) is gitignored

## Project layout

```
src/x_automation/
  auth.py        # OAuth2 PKCE setup
  cli.py         # CLI entry point
  config.py      # env, paths, client factory
  delete.py      # delete loop + checkpoints
  drafts.py      # local draft queue
  filters.py     # emoji/tag filtering
  grok.py        # xAI Grok transforms
  inbox.py       # Cursor agent JSON import
  ingest.py      # RSS + Grok ingest orchestration
  publish.py     # publish approved drafts + daily caps
  rate_limit.py  # 429 handling + pacing
  rss.py         # RSS feed polling
  timeline.py    # fetch user posts + inventory export
```
