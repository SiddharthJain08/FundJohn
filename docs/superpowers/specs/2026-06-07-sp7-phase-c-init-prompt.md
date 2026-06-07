# SP-7 Phase C — init prompt (paste into a fresh session to start the phase)

**Written 2026-06-07 at Phase B activation.** Parent spec §5:
`docs/superpowers/specs/2026-06-04-sp7-universe-expansion-design.md`.
House rule: re-ground every named fact below against live state at YOUR plan
time before trusting it (feedback_spec_plan_codebase_grounding).

---

## The prompt

You are starting **SP-7 Phase C — Live Wiring**: make per-strategy universes
LIVE in the trading engine, re-point the collector's daily fetch envelope to
the resolver union, and audit every universe consumer. Phases A (data
foundations) and B (tier-ladder backtest + adoption + threshold proposals) are
complete and activated. Follow superpowers brainstorming → spec → plan →
subagent-driven build; every gate default-OFF; shadow-parity before any flip.

### Entry conditions (verify before brainstorming)

1. Phase B's full ladder run COMPLETE: redis `sp7:ladder:last_full_run` set;
   `universe_ladder_runs` has a drained run (no queued/running cells).
2. Operator has decided adoptions (✅ reactions / :7870 buttons / deliberate
   no-change). Check `strategy_universe_recommendations` rows with
   `candidate_set_id LIKE 'sp7b-%'` — adopted=true count tells you how many
   strategies carry non-default `universe_filter_ref` in the manifest.
3. `python3 -m system_checks --check universe_tier_coherence` → PASS
   (B0 repair held; if FAIL, STOP — the metadata regressed).

### What Phase C delivers (parent spec §5, re-ground at plan time)

- **C1 — engine per-strategy universes live.** Signals step builds each
  strategy's universe via `UniverseResolver` + manifest `universe_filter_ref`;
  gate `OPENCLAW_LIVE_UNIVERSE_RESOLVER` default-OFF. Rollout: shadow-parity
  ≥3 trading days logging resolved-vs-clamped diffs per strategy; flip
  requires ZERO signal-delta for un-adopted (sp500) strategies. THEN delete
  the A4 clamp (`OPENCLAW_ENGINE_UNIVERSE_CLAMP` + `universe_clamp.py` —
  delete the code, don't gate it off). Engine memory invariant: ONE union
  price panel load, sliced per strategy — never N panel loads.
- **C2 — collector envelope.** `readUnionUniverseFromRedis` (collector.js,
  exported with ZERO callers since SP-2) finally gets its caller: daily
  equity fetch list := resolver union ∪ benchmarks/sector ETFs ∪
  `universe_config` operator overlay (active=false stays a hard exclusion).
  Coverage-floor DECOUPLING: the fetch envelope must NOT apply has_floor —
  that gate is for strategy resolve only; newly adopted tiers must get their
  data fetched (kills the chicken-and-egg permanently).
- **C3 — consumer audit.** Walk every universe reader (sentiment step,
  options archive, redeploy pipeline, screener, doctor, system_checks) and
  assert which envelope each consumes; fundamentals/insider fetchers scope to
  adopted-union (parent decision 4); extend doctor coverage checks to the 5k
  envelope. One explicit envelope-assertion test per consumer.

### Phase-B-vintage facts Phase C must know (verified 2026-06-07)

- **Live resolve is SLOW**: a 67-strategy union resolve takes 30–50 s wall on
  the loaded 2-core box (measured twice: 31.3 s, 49.6 s). Root causes:
  `fetch_metadata_as_of` opens a NEW psycopg2 conn per call and
  `ParquetCoverage._load_month` re-reads all of prices.parquet (~7.5 M rows)
  per month-miss. The `universe_resolution` system_check (15 s gate) ALREADY
  FAILs under load. **C1 should fix this**: share one DB conn + reuse the
  hoisted `CoverageIndex` from `scripts/build_tier_membership.py` (one
  parquet read, (ticker × month) cumulative counts) in the live path.
- **PrecomputedResolver is backtest-only** (`src/backtest/precomputed_resolver.py`)
  — frozen artifacts are PIT-correct for backtests but WRONG for live (live
  needs today's snapshot). Don't reuse it for C1; reuse `CoverageIndex`.
- Tier predicates: `CANDIDATE_PREDICATES` has 16 entries; the 4 ladder tiers
  are in `LADDER_TIER_PREDICATES` (adoption-only — NOT in the PaperHunter
  mint menu; exposing them at mint is a Phase D decision).
- The engine clamp currently keeps ≈591 (503 SP500 + 33 ETF + conventions +
  dash→dot fix ab4238f). The clamp is DELETED at the end of C1, not gated.
- B0 re-bounded acceptance: historical in_sp500 ceiling ≈476
  (Wikipedia-CSV gap; backlog = fuller SP500 membership history, then re-run
  `scripts/sp7_b0_repair_metadata.py --months --resume` — idempotent).
- `tests/test_sp2_smoke.py::test_system_checks_pass` carries a pre-B0
  exemption for `universe_tier_coherence` — if B0 acceptance passed it MUST
  be removed (runbook §2d); verify it was.
- Discord: rec posts need `agent_registry.webhook_urls['universe-recs']` or
  `DISCORD_UNIVERSE_RECS_WEBHOOK` env (was MISSING live at Phase B
  activation — check it got wired, runbook §4).
- Box: 2-core/8 GB/no-swap. Monolithic pytest of the full suite OOMs at
  76–94% — run chunked per-file. Sequential subagents only.
- Phase D (NOT yours): legacy universe-recs mode + gate removal, mint-time
  ladder (budget risk vs saturday-brain 6 h ceiling), PaperHunter tier menu,
  options_eligible chain-probe backlog, Universe dashboard page.

### Key file map

| What | Where |
|---|---|
| Parent spec / B spec / B plan / B runbook | docs/superpowers/specs/2026-06-04-sp7-universe-expansion-design.md · 2026-06-06-sp7-phase-b-tier-ladder-design.md · plans/2026-06-06-sp7-phase-b-tier-ladder.md · docs/sp7-phase-b-runbook.md |
| Resolver + adapters | src/strategies/universe_resolver.py · _db_adapters.py (per-call conn + per-month parquet scan = the C1 perf targets) |
| Engine fallback universe + clamp | src/execution/engine.py:~1396 · src/execution/universe_clamp.py |
| Collector envelope | src/pipeline/collector.js (getActiveUniverse caller ~1392; readUnionUniverseFromRedis ~146, zero callers) |
| Ladder runner / driver / selection / recs | src/backtest/{universe_grid_cli,universe_ladder_selection,universe_ladder_recs,precomputed_resolver}.py · scripts/run_universe_ladder.py |
| Coherence guard | src/system_checks/checks/universe_tier_coherence.py |
| Memory anchors | project_sp7_phase_b_tier_ladder · project_sp7_universe_expansion · feedback_spec_plan_codebase_grounding · reference_vps_two_core_cpu |

### Conventions

TDD per task; subagent-driven with two-stage review; spec → operator approval
→ plan → build; grep-verify every named symbol before committing the plan;
nightly heavy work inside 01:00–13:00 UTC Mon–Fri only; never DELETE from
master data; shadow-parity proof before any live flip; the operator remains
the gate for merge + activation.
