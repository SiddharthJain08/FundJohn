# SP-4 Session Init Prompt

Paste the block below into a fresh Claude Code session (run from `/root/openclaw`) to begin SP-4. CLAUDE.md + auto-memory load automatically, so this only needs to orient toward SP-4 and point at the handoff.

---

```
We're starting SP-4 (Weekly Research Uplift) for FundJohn/OpenClaw.

Read the handoff first: docs/superpowers/specs/2026-05-26-sp3.1-handoff.md
It covers the full state — SP-2 (universe), SP-3 (asset-class rails), and SP-3.1
(crypto) are ALL complete and LIVE on this VPS. SP-4 builds on all three.

SP-4 goal: teach the Saturday research stack (corpus curator + PaperHunter swarm
+ StrategyCoder + MasterMind reviewer) that the broader SP-2 universe and the new
SP-3/3.1 asset classes (options, etp, crypto) are in scope — so it can ORIGINATE
non-equity strategies end-to-end, not just equity-momentum. Likely scope (confirm
in brainstorming): PaperHunter implementability gate accepts options/crypto/
commodity papers + infers instrument_class at mint; StrategyCoder per-asset-class
templates (emit correct instrument_class + requirements.json + registry mapping);
MasterMind corpus filters; calibrate the option PROMOTION_THRESHOLDS placeholder
(TODO(SP-4)) + revisit crypto's 0.70; prove PaperHunter→StrategyCoder→backtest
works for a non-equity archetype; verify Sat-18:00 review + Sat-19:00 position-recs
are asset-class-aware. Open question to decide early: is options-greeks support
(SP-3 deferred greeks-aware option sizing/backtest) a prerequisite, a sibling
SP-3.2, or in-scope for SP-4?

Process: brainstorm → spec → plan → subagent-driven execution in a git worktree
(superpowers skills). One phase/sub-project at a time. Ground every named
convention against live source before dispatching subagents.

Hard constraints (live VPS, real paper money):
- Surface before any merge/deploy; surface paper-order/live ops for OK before firing.
- Confirm LLM-budget headroom before heavy subagent cycles (the Saturday brain is
  already a 4-6h job; Opus 4.7 1M passes ~$8/call).
- NEVER delete from the master DB (append-only parquets + canonical Postgres tables).
- Never git add -A / never commit secrets (.env* gitignored — stage specific files).
- psql is NOT installed — apply migrations via psycopg2; verify (the startup
  migrate() runner has a non-idempotent skip wart).
- Worktree: symlink data/master from /root/openclaw (gitignored); grep POSTGRES_URI
  / ALPACA_* from .env, don't source it.

Don't start coding until we've brainstormed and I've approved a design. Begin by
reading the handoff, then propose how to decompose SP-4 (it may want phases like
SP-2/SP-3.1 did) and ask me the first scoping question.
```

---

**Notes for the operator:**
- First confirm the SP-3.1 first-trade verification landed (Discord post / `/root/crypto-first-trade-verify.log` after 2026-05-27 15:00 UTC) before piling SP-4 changes onto the live research stack.
- If SP-4 turns out large, the natural first sub-project is the **PaperHunter implementability gate + instrument_class-at-mint** (smallest, unblocks everything downstream); StrategyCoder templates + threshold calibration follow.
