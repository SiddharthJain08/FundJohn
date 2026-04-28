---
name: fundjohn:obsidian-link
description: Generate Obsidian-format wikilinks between research artifacts (memos, notes, theses).
triggers:
  - cross-link memo references
  - vault hygiene pass
inputs:
  - source_artifact
  - target_artifacts
outputs:
  - wikilinks_block
keywords: [obsidian, vault, wikilink, research-graph]
---
# Skill: fundjohn:obsidian-link
**Trigger**: `/obsidian` or `/note`

## Purpose
Every markdown note an agent writes into `workspaces/default/` must follow the Obsidian-vault convention so (a) humans can navigate via backlinks + graph view, (b) pgvector retrieval can filter by frontmatter before cosine search, and (c) other agents can read just the frontmatter when scanning.

This skill is universal — every agent that writes notes should have it.

## Rules

### 1. Always start with YAML frontmatter
The set of allowed `type` values is closed: `paper | strategy | position | thesis_checkin | weekly_review | morning_note | initiating`. If your note doesn't fit one, escalate before inventing.

```yaml
---
type: <required closed enum>
date: <ISO date or datetime>
tags: [<required, see taxonomy>]
... type-specific fields per template ...
---
```

Templates live at `workspaces/default/_templates/` — copy and fill, never write blank.

### 2. Use [[wikilinks]], not relative paths
- `[[strategy-momentum-12m]]` not `../strategies/momentum-12m.md`
- `[[initiating-AAPL]]` not `results/initiating-AAPL.md`
- A position memo MUST link to its parent strategy and parent thesis.
- A thesis check-in MUST link to its initiating memo.
- A strategy memo MUST link to its parent paper(s) if any.

The graph is the index. Don't break it with raw paths.

### 3. Tag taxonomy (closed set)
Top-level namespaces only — see `_templates/README.md`. Allowed:
`#paper #strategy #position #thesis-checkin #weekly-review #morning-note #initiating`
`#factor/* #asset/* #ticker/* #sector/* #state/* #regime/* #conviction/* #status/*`

**YAML caveat — write tags WITHOUT the `#` prefix in frontmatter.** The `#` is
the *display* form (what Obsidian's tag pane shows). In YAML, an unquoted `#`
starts a comment, so `tags: [#strategy]` parses as `tags: []`. Correct:

```yaml
tags: [strategy, strategy/BS03_options_mispricing, state/live, ticker/AAPL]
```

Obsidian and Dataview prepend the `#` automatically when matching against
queries like `FROM #strategy`.

Inventing a new top-level tag without updating `_templates/README.md` is a quality-gate failure.

### 4. File location rules
- Papers → `workspaces/default/papers/<slug>.md`
- Strategies → `workspaces/default/strategies/<name>.md`
- Position memos → `workspaces/default/results/positions/<TICKER>.md`
- Weekly reviews → `workspaces/default/results/reviews/<YYYY-MM-DD>.md`
- Initiating coverage → `workspaces/default/results/initiating-<TICKER>.md`
- Thesis check-ins → `workspaces/default/results/thesis-<TICKER>.md` (append, don't overwrite)

### 5. Token discipline
- Notes ≤ 1,500 words unless the type-specific skill (e.g. `comprehensive-review`) explicitly allows more.
- Front-load the TL;DR / hypothesis / thesis paragraph — agents reading downstream may only inject the first 500 tokens.

## Anti-patterns (auto-reject)
- Note without frontmatter
- `type:` outside the closed enum
- Relative-path links instead of `[[wikilinks]]`
- Inventing a new top-level tag
- Position memo without parent strategy + parent thesis links
- Note >1,500 words without skill-specific allowance
