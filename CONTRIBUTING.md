# Contributing

FundJohn is a single-VPS production system: the working tree at
`/root/openclaw` on branch `main` **is** the deployment. Read
[docs/bootstrap.md](docs/bootstrap.md) to stand up a development replica, and
[ARCHITECTURE.md](ARCHITECTURE.md) before touching anything on the trading
path.

## Testing

```bash
pytest -m 'not integration' -q     # unit suite (integration tests need live Postgres)
npm run test:js                    # node --test over tests/**/*.test.js
python3 -m system_checks           # live-state probes (running system only)
node scripts/smoke/graph-smoke.js  # manual smokes: scripts/smoke/
```

CI (`.github/workflows/tests.yml`) runs the JS tests and the non-integration,
non-sentiment pytest set. On the 2-core production box, always run test/backtest
work serialized and `nice -n 19`.

## Conventions (descriptive — match what exists)

- **Python**: `snake_case` modules; rationale-dense module docstrings (the
  headers in `src/execution/` and `src/backtest/` are the standard — say
  *why*, cite dates/decisions).
- **Strategies never raise.** `generate_signals` is pure Python over
  pre-loaded frames; missing data returns an empty list. No network, DB,
  filesystem, or LLM imports in `src/strategies/implementations/` —
  an `import requests` there is an auto-reject.
- **Logging is print-to-journald** (deliberate — no logging framework on the
  2-core box). Broad `except Exception` fail-open is allowed on the trading
  path ONLY with a machine-greppable tag; silent rc=0 no-ops have caused
  multi-day incidents.
- **Migrations**: numbered SQL in `src/database/migrations/`, append-only —
  never edit an applied migration; add a new one.
- **Master data is append-only** — see the NEVER-DELETE invariant in
  [CLAUDE.md](CLAUDE.md). Deprecation is a flag, never a `DELETE`. Parquet
  writers write atomically (tmp + `os.replace`).
- **Commits**: conventional-commit subjects (`feat:`, `fix:`, `docs:`,
  `cleanup:`, `security:`) with scope in parentheses where useful.
- **Specs/plans**: grep-verify every named env var, function signature, and
  path against the codebase before writing it down.
- **Docs**: current docs live at root + `docs/{runbooks,reference}`; anything
  dated goes to `docs/archive/`. New changelog entries go in
  `docs/archive/changelog.md`, newest first.

## Deployment notes

- Long-running services (johnbot + its :3000 dashboard, fundjohn-dashboard,
  mastermind-chat, finbert) pick up code **only on restart**; timer-spawned
  scripts pick up the working tree on their next fire — never leave the tree
  half-edited across a timer boundary.
- johnbot is ROOT USER scope: `XDG_RUNTIME_DIR=/run/user/0 systemctl --user
  restart johnbot`. Never start the system-scope copy.
- After editing CLAUDE.md / AGENTS.md / IDENTITY.md / SOUL.md run
  `npm run integrity:generate` (they are hash-watched at boot).
