# FundJohn Workspace — Vault Map

Open this in Obsidian and use it as your home note. Every other directory below has a specific purpose. Agents read and write here too — this isn't a pure human-curated vault.

## Layout

| Folder | What's in it | Who writes |
|--------|-------------|------------|
| `agent.md` | Operational frameworks, lessons learned, architecture decisions | Operator + agents |
| `memory/` | Rolling logs (`fund_journal.md`, `active_tasks.md`, `regime_context.md`, `signal_patterns.md`, `trade_learnings.md`) | `memory-writer.js` (JS) + Python pipelines |
| `results/` | Structured outputs: strategy memos, IC notes, thesis check-ins, weekly reviews | Agents (researchjohn, mastermind, strategycoder) |
| `results/strategies/` | Per-strategy reviews and deployment reports | Mastermind, strategycoder |
| `strategies/` | Strategy *implementations* (Python files + manifest). Read-only from the vault perspective | Strategycoder, manual edits |
| `data/` | Master parquet datasets (don't try to open in Obsidian) | Data pipeline |
| `_templates/` | Frontmatter templates — `Templater` core plugin pulls from here | Stable; edit only the canonical copy in `src/workspace/template/_templates/` |
| `tools/` | Auto-generated Python MCP modules (per-cycle data fetchers). Don't edit | `registry.js` at boot |
| `tmp/` | Scratch space for in-flight subagents | Subagents |
| `.agents/` | Per-thread evicted history + large tool results | `context-management.js` |
| `.obsidian/` | This vault's settings — yours to customize | You |

## How to navigate

- **Graph view** (cmd-G / ctrl-G) — node colors are tag-coded: 🟢 strategy, 🔵 paper, 🟠 position, 🟡 initiating, 🟣 thesis-checkin
- **Quick switcher** (cmd-O / ctrl-O) — fuzzy-jump by note name
- **Tag pane** (cmd-shift-T) — filter notes by `#strategy/{id}`, `#ticker/{TICKER}`, `#regime/{state}`, etc.
- **Backlinks** sidebar — see which other notes link here
- **Search** — full-text + frontmatter via `tag:#strategy ticker:AAPL` etc.

## Tag taxonomy (closed set)

See `_templates/README.md` for the canonical list. Short version:

- **Note types**: `#paper #strategy #position #thesis-checkin #weekly-review #morning-note #initiating`
- **Factor**: `#factor/{momentum|value|quality|volatility|reversal|sentiment|macro|event}`
- **Asset**: `#asset/{equities|options|futures|fx|rates|crypto|multi}`
- **Ticker**: `#ticker/{TICKER}` — uppercase
- **State**: `#state/{candidate|paper|live|monitoring|deprecated}`
- **Regime**: `#regime/{LOW_VOL|TRANSITIONING|HIGH_VOL|CRISIS}`

Don't invent new top-level tags — agents won't recognize them and your graph will fragment.

## Recommended community plugins

Install via Settings → Community plugins. Suggested:

- **Dataview** — query frontmatter as tables: `\`\`\`dataview\nTABLE conviction FROM #initiating WHERE conviction > 0.7\n\`\`\``
- **Templater** — programmable templates (already configured to use `_templates/`)
- **Tag Wrangler** — rename tags safely
- **Recent Files** — sidebar list of recently-edited notes (useful since agents write a lot)
- **Frontmatter Title** — render note titles from frontmatter when filenames are dated (e.g. `BS03_options_mispricing-deployed-2026-04-09.md`)

## Things NOT to do

- Don't move files outside their canonical directories — agents resolve `[[wikilinks]]` by name but path conventions matter for retrieval filters.
- Don't edit `tools/*.py` — regenerated at every johnbot boot.
- Don't delete `memory/` files — they're append-only operational logs.
- Don't edit `_templates/` here — those are mirrored from the tracked `src/workspace/template/_templates/`. Edit the source.

## Where the rest of the system is

This vault is one part. The rest:

- **Discord bot** — johnbot.service (PTC + flash routing)
- **Dashboard** — http://127.0.0.1:7870 (live trace, HITL approve/veto, P&L curves)
- **Research tab** — http://127.0.0.1:3000/research (MastermindJohn chat backed by Opus 4.7 1M)
- **Postgres** — `verdict_cache`, `trades`, `memory_chunks` (this vault's pgvector index), etc.
- **Plan + LEARNINGS** — `/root/.claude/plans/go-through-this-deep-wand.md`, `/root/.learnings/LEARNINGS.md`
