# x-automation

CLI tools for your X account: bulk delete, emoji-targeted delete, and approve-first posting from RSS/Grok/Cursor drafts. LinkedIn is separate: industry posts are generated and published on a schedule with no approval step.

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

## LinkedIn autopost

LinkedIn posts go out on their own. There is no approve step. X drafts are unchanged and still require `drafts approve` before `drafts publish --execute`.

The publisher uses LinkedIn's official APIs only:

- OAuth 2.0 authorization-code (3-legged) flow
- scopes `openid profile w_member_social`
- member id from `GET https://api.linkedin.com/v2/userinfo` (`sub`)
- `POST https://api.linkedin.com/rest/posts` with author `urn:li:person:<id>`
- headers `LinkedIn-Version` (current `YYYYMM`, `202611` as of November 2026) and `X-Restli-Protocol-Version: 2.0.0`

Personal access tokens last about 60 days. LinkedIn returns a refresh token only if the app is approved for programmatic refresh. This project uses a refresh token when one is present. Otherwise it stops on expiry or HTTP 401 and the GitHub Actions job fails with `LINKEDIN AUTH FAILURE`.

### Industries

Edit `linkedin_feeds.yaml` (tracked; Actions uses it) and `linkedin_prompts.yaml`. The `.example` files are the same starter. Shipped topics:

| Id | Weight | What it posts |
|----|--------|----------------|
| `fedramp` | 3 | FedRAMP authorizations, GovCloud, federal cloud, and the defense / AWS connection when the source supports it |
| `aerospace_defense` | 1 | Aerospace, space, and defense |
| `tech` | 1 | Software and infrastructure |
| `aws_cloud` | 1 | AWS integration and cloud infrastructure, including FedRAMP when the source says so |

Weight 3 on FedRAMP produces this cycle, then it repeats:

`fedramp`, `aerospace_defense`, `fedramp`, `tech`, `fedramp`, `aws_cloud`

About half of posts are FedRAMP. The other three rotate through the remaining slots. Set `enabled: false` or change `weight` to shift that. Prompts are told never to invent an authorization status, impact level, or customer.

Each run publishes at most one post, skips items already in `state/linkedin_posted.json`, and stops at 2 posts per America/New_York day or when the previous post was under 3 hours ago.

### One-time LinkedIn app setup

1. Open [LinkedIn Developer Apps](https://www.linkedin.com/developers/apps) and create an app.
2. On the Products tab, add both:
   - **Share on LinkedIn** (scope `w_member_social`)
   - **Sign In with LinkedIn using OpenID Connect** (scopes `openid` and `profile`)
3. On the Auth tab, add this redirect URL exactly:

   `http://127.0.0.1:8765/callback`

4. Copy the client id and client secret into `.env`:

```bash
cp .env.example .env
# LINKEDIN_CLIENT_ID, LINKEDIN_CLIENT_SECRET, LINKEDIN_REDIRECT_URI
# XAI_API_KEY is also required (Grok writes the post)
```

5. Authorize your member account (this is local and interactive; Actions cannot do it):

```bash
python -m x_automation.cli linkedin auth
```

The browser consent screen must include `openid`, `profile`, and `w_member_social`. The command writes `data/linkedin_token.json` (gitignored) and prints the expiry and whether a refresh token was granted. It does not print the token.

6. Try a post without publishing (this still calls xAI):

```bash
python -m x_automation.cli linkedin autopost --dry-run
python -m x_automation.cli linkedin autopost --dry-run --industry fedramp
```

7. Publish once from your machine if you want to confirm the API before the schedule takes over:

```bash
python -m x_automation.cli linkedin autopost
```

### GitHub secrets

Repository secrets (Settings → Secrets and variables → Actions). Never commit the token file.

| Secret | Required | Value |
|--------|----------|--------|
| `LINKEDIN_TOKEN_JSON` | Yes, to publish | Entire contents of `data/linkedin_token.json` |
| `XAI_API_KEY` | Yes | xAI API key used to write posts |
| `LINKEDIN_CLIENT_ID` | Only if a refresh token was granted | LinkedIn app client id |
| `LINKEDIN_CLIENT_SECRET` | Only if a refresh token was granted | LinkedIn app client secret |

```bash
gh secret set LINKEDIN_TOKEN_JSON < data/linkedin_token.json
gh secret set XAI_API_KEY
gh secret set LINKEDIN_CLIENT_ID
gh secret set LINKEDIN_CLIENT_SECRET
```

`XAI_MODEL` is not a secret. The workflow uses `grok-3-mini`.

### Schedule

`.github/workflows/linkedin-autopost.yml` runs on weekdays:

| Cron (UTC) | Winter (EST) | Summer (EDT) |
|------------|--------------|--------------|
| `15 13 * * 1-5` | 8:15 AM | 9:15 AM |
| `30 17 * * 1-5` | 12:30 PM | 1:30 PM |

GitHub cron does not follow daylight saving time. Both windows are still morning and midday Eastern, which is when LinkedIn engagement is strongest. GitHub may start a scheduled job late. The 3-hour gap skips a second post if the two runs land on top of each other.

You can run it immediately with **Actions → LinkedIn autopost → Run workflow**. Check "Generate a post without publishing" for a dry run.

After a successful publish, the workflow commits `state/linkedin_posted.json` so the next run does not repeat the story. That commit needs permission to push to the default branch. If branch protection blocks the GitHub Actions bot, the post can still go out and the job then fails; fix the ruleset (or allow that bot to update `state/linkedin_posted.json`) so the next run does not post the same item again.

### When the token expires

The Actions log shows a `LINKEDIN AUTH FAILURE` block and the job is marked failed. The failure step also raises a GitHub error annotation.

1. On your machine: `python -m x_automation.cli linkedin auth`
2. Update the secret: `gh secret set LINKEDIN_TOKEN_JSON < data/linkedin_token.json`
3. Re-run the **LinkedIn autopost** workflow.

If the saved token includes `refresh_token` and the client id and secret secrets are set, the workflow refreshes an access token that is expired or inside its last day. The refreshed access token is used for that run. The secret can keep the previous JSON until refresh itself is rejected (refresh tokens are typically a year, and LinkedIn can revoke them). When refresh fails, do the same re-auth steps.

Image posts are optional. Set `attach_images: true` in `linkedin_feeds.yaml` to upload a feed image through the Images API (`initializeUpload`, then the post `content.media.id`). A failed upload still publishes the text.

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
