# Workspace Templates

These templates back the Obsidian vault overlay on `workspaces/default/`. Agents must use them when writing to:

- `papers/` → `paper-note.md`
- `strategies/` → `strategy-memo.md`
- `results/positions/` → `position-memo.md`
- `results/reviews/` → `weekly-review.md`
- `results/initiating-{ticker}.md` → schema enforced by `fundjohn:initiating-coverage` skill
- `results/thesis-{ticker}.md` → schema enforced by `fundjohn:thesis-tracker` skill

## Why frontmatter is mandatory

Every note's YAML frontmatter is the index. Three things use it:

1. **Obsidian Dataview** — humans run live queries (`TABLE confidence FROM #paper WHERE confidence > 0.75`)
2. **pgvector retrieval** (migration 054) — `type` and `tags` filter the search before cosine similarity
3. **Cross-cycle agent reads** — instead of slurping a full file, an agent reads only the frontmatter when scanning

If you write a note without frontmatter you have created a black-hole.

## Wikilinks

Use `[[Note Name]]` not relative paths. Obsidian resolves them; agents resolve them via `fundjohn:obsidian-link` skill. The graph view depends on it.

## Tag taxonomy

Stable top-level tag namespaces:
- `#paper`, `#strategy`, `#position`, `#thesis-checkin`, `#weekly-review`, `#morning-note`, `#initiating`
- `#factor/{momentum|value|quality|volatility|reversal|sentiment|macro|event}`
- `#asset/{equities|options|futures|fx|rates|crypto|multi}`
- `#ticker/{TICKER}`
- `#sector/{tech|financials|...}`
- `#state/{candidate|paper|live|monitoring|deprecated}`
- `#regime/{LOW_VOL|TRANSITIONING|HIGH_VOL|CRISIS}`
- `#conviction/{up|down|flat}`
- `#status/{open|closed|scaled|pending_approval}`

Do not invent new top-level tags without updating this file.
