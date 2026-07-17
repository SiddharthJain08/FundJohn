# SP-5 Init Prompt

*(Paste the block below into a fresh Claude Code / BotJohn session to begin SP-5. It assumes the SP-4 work is merged + live on `main`.)*

---

We're starting **SP-5 — the final piece of the asset-class / research expansion**.

**Read first, in order:**
1. `docs/superpowers/specs/2026-05-27-sp4-handoff.md` — full program state (SP-1…SP-4), what's live, the SP-5 gap, operating constraints, and SP-4 lessons learned. This is your primary brief.
2. Your memory `project_sp4_weekly_research_uplift` (complete SP-4 state) and `project_sp31_crypto` (the crypto execution lane — the pattern SP-5's options-execution work parallels).

**The core of SP-5 (the loop to close):** SP-4 made the Saturday research stack *originate* non-equity strategies (option/etp/crypto) — that's LIVE (gate `OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT=1`). But:
- **Options can't be traded live.** `OptionSpec` is consumed only by the backtest; `alpaca_executor.py` has no option order-submission path. An originated option strategy can be a candidate + backtest on the greeks engine, but cannot be promoted-to-live-and-traded.
- **`futures` is the last unrouted `instrument_class`** (still raises `NotImplementedError`).

The handoff §4 lays out the candidate SP-5 scope: (1) **options live execution lane** (PRIMARY — parallel to SP-3.1's crypto exec lane, consuming `OptionSpec`; starts with an Alpaca-options-CLI grounding/snapshot to confirm the account is options-trading-enabled and capture the order surface); (2) **`futures` rails** (secondary, data-blocked); (3) **data unlocks** (real options chains → single-name/OTM; crypto microstructure; leveraged-ETP decay); (4) soak-verify the first post-activation Saturday origination.

**Process (mandatory):** superpowers brainstorm → spec → writing-plans → subagent-driven-development, in a git worktree (native `EnterWorktree`), one sub-project at a time, each with its own brainstorm→spec→plan cycle. Ground every named convention (env var, CLI flag, signature, file path) against live source before dispatching subagents. Don't start coding until we've brainstormed and I've approved a design.

**Hard constraints (carry verbatim):**
- Surface before any merge/deploy; surface paper-order/live-ops for OK before firing; confirm LLM-budget headroom before heavy subagent cycles.
- NEVER delete from the master DB (append-only parquets + canonical Postgres tables: `execution_signals`, `signal_pnl`, `alpaca_submissions`, the master `*.parquet` family). Columns/tickers ADD only; deprecation = `active=false`, never DELETE. (`strategy_registry` is not master.)
- Never `git add -A` / never commit secrets. `.env*` is gitignored — stage specific files. Edit `.env` via `printf >>` only (never read it into context, never create `.env.bak` — per the 2026-05-25 credential-leak incident). After editing a tracked, manifest-covered file, run `./scripts/regen-integrity-manifest.sh` on the VPS — do NOT commit the manifest.
- psql is NOT installed — apply migrations via psycopg2 and verify (the `migrate()` runner has a non-idempotent skip wart). `POSTGRES_URI`/`ALPACA_*` are in `.env` — grep them, don't `source` (unquoted parens break bash).
- Worktree: `data/master` is gitignored → `ln -s /root/openclaw/data/master <worktree>/data/master`. For any real-run proof: **force `OPENCLAW_DIR` to the worktree** (prod `.env` points it at `/root/openclaw`/main), add new emitted fields to the SKILL.md schemas (not just prompts), and `chown` the worktree to `claudebot` if the subagent must write it (don't escalate the subagent to root).

**Start by:** reading the handoff, then propose how to decompose SP-5 (likely: options-execution sub-project first), confirm whether the Alpaca account supports options/multi-leg trading is a grounding gate, and ask me the first scoping question. Default the task weight to balanced (50/50 token/execution) unless I say otherwise.
