# Cursor Agent Draft Inbox

Cursor agents (or any external tool) can add drafts to the approve-first queue **without X API credentials**.

## Workflow

1. Agent writes a JSON file to `data/inbox/cursor/`
2. You run: `python -m x_automation.cli drafts import-inbox`
3. Review: `python -m x_automation.cli drafts list --status pending`
4. Approve: `python -m x_automation.cli drafts approve <id>`
5. Publish: `python -m x_automation.cli drafts publish --execute`

Processed files move to `data/inbox/cursor/processed/`.

## Draft JSON format

See [draft.schema.json](../../inbox/cursor/draft.schema.json).

Example `data/inbox/cursor/comedy-001.json`:

```json
{
  "category": "comedy",
  "tag_emoji": "👽",
  "text": "Local man discovers cloud computing is just someone else's computer. Experts recommend denial.",
  "metadata": {
    "agent": "cursor-comedy",
    "prompt_version": "1"
  }
}
```

Example with URL (higher X API cost when published):

```json
{
  "category": "news",
  "tag_emoji": "🔴",
  "text": "Major policy shift announced today.",
  "source_url": "https://example.com/story",
  "metadata": {
    "agent": "cursor-news"
  }
}
```

If `source_url` is set, append it to the post text manually in `text`, or rely on RSS ingest for automatic URL handling.

## Cursor Automation prompt (starter)

Use in a Cursor Automation or agent task:

```
Read the user's topic or attached RSS snippet. Write ONE draft post as JSON matching
the schema in inbox/cursor/draft.schema.json. Save to:
  /Users/kylehamwey/Projects/x-automation/data/inbox/cursor/{timestamp}-{category}.json

Rules:
- category must be one of: news, manufacturing, comedy
- max 260 chars in text (emoji tag added separately)
- do NOT call the X API
- comedy: absurdist dry tone; manufacturing: operator insight; news: neutral brief
```

Replace the path with your machine's x-automation project path.

## Cost note

Cursor usage is separate from X API credits. Drafts only cost X credits when you run `drafts publish --execute`. Posts containing `https://` cost roughly $0.20 each vs ~$0.015 for text-only.
