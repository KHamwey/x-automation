# x-automation

Programmatic tools for managing your X account — starting with safe bulk deletion of posts and replies.

## Prerequisites

- Python 3.9+
- An [X Developer Console](https://console.x.com) app with OAuth 2.0 credentials
- API credits loaded in the Developer Console (pay-per-use; see cost notes below)
- Scopes: `tweet.read`, `tweet.write`, `users.read`, `offline.access`

## Setup

```bash
cd x-automation
python3 -m pip install .
cp .env.example .env
# Edit .env with CLIENT_ID, CLIENT_SECRET, REDIRECT_URI from Developer Console
```

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
  filters.py     # emoji/tag filtering
  rate_limit.py  # 429 handling + pacing
  timeline.py    # fetch user posts + inventory export
```
