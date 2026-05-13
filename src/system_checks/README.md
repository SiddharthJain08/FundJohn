# system_checks

Runnable diagnostic probes for live OpenClaw state. **These are not unit tests** — they verify the production system is functioning correctly *right now*.

## Quick start

```bash
# Run every check
python3 -m system_checks

# Filter by tag
python3 -m system_checks --tag pipeline broker

# Run one specific check
python3 -m system_checks --check pipeline_completed_today

# Machine-readable output for agents
python3 -m system_checks --json

# List all registered checks (without running)
python3 -m system_checks --list
```

Exit codes mirror `src/maintenance/doctor.py`:
- `0` — all checks PASS or SKIP
- `1` — at least one WARN, none FAIL/ERROR
- `2` — at least one FAIL or ERROR

## Library API

```python
from system_checks import run_one, run_all, summarize, Status

# Run a subset
results = run_all(tags=['pipeline'])
# Or one
result = run_one('alpaca_session_authed')
assert result.status is Status.PASS, result.detail
```

## Adding a new check

Drop a function into the appropriate `checks/*.py` module (or create a new domain file and `import` it from `checks/__init__.py`). Decorate with `@check`:

```python
from ..registry import check
from ..types import Status

@check(name='my_new_probe', tags=['pipeline'], requires=['db'])
def _my_new_probe():
    # ... do the check, decide ...
    return Status.PASS, 'one-line detail string'
```

### The contract

- **Return** `(Status, str)`. `Status` is one of `PASS / WARN / FAIL / SKIP / ERROR`. The string is shown in human output and the `detail` field of JSON. Keep it under 200 chars.
- **`name`** is unique across the whole registry. Snake-case. Describes the *invariant being checked*, not the implementation.
- **`tags`** — a list. Domain tags so maintenance can run subsets: `pipeline`, `broker`, `regime`, `strategies`, `agents`, `storage`.
- **`requires`** — what infrastructure the check needs. The runner SKIPs the check (doesn't fail it) when a dep isn't available. Valid keys:
  - `fs` — filesystem (always available)
  - `db` — `POSTGRES_URI` env set + container reachable
  - `broker` — `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` env set
  - `llm` — `/usr/local/bin/claude-bin` exists
  - `discord` — `DISCORD_BOT_TOKEN` env set
- **Don't raise** — but if you do, the runner converts it to `ERROR` and captures the traceback. Prefer explicit `return Status.FAIL, str(e)`.
- **Keep it fast** — under 5s per check in the typical case. Heavier integration probes go in their own file with a `heavy` tag so they can be excluded by maintenance.

### When to add a check

Every time you fix a class of bug that *shouldn't recur*, add a regression probe. Many existing checks were seeded from the 2026-05-13 zero-signal cycle audit — each FAIL we hit becomes a check so future maintenance catches it on the next run.

## Architecture

```
src/system_checks/
├── __init__.py        # public API
├── __main__.py        # `python3 -m system_checks` entry
├── registry.py        # @check decorator + storage
├── runner.py          # run_one, run_all, dep-skip, error capture, timing
├── types.py           # Status enum, CheckResult dataclass
├── cli.py             # argparse + human/JSON formatters
└── checks/
    ├── __init__.py    # side-effect imports each domain module
    ├── pipeline.py
    ├── broker.py
    ├── regime.py
    ├── strategies.py
    ├── agents.py
    └── storage.py
```

The framework is exercised by `tests/test_system_checks_framework.py`. Individual checks are exercised by running them against live state (`python3 -m system_checks`).

## Where this fits

- **`src/maintenance/doctor.py`** — fast preflight (sub-second per check; runs as systemd `ExecStartPre` and pipeline preflight). Aborts a cycle that's about to start.
- **`src/system_checks/`** (this) — deeper post-cycle probes (DB queries, broker round-trips, strategy imports). Catches bugs that doctor's preflight can't see. Invoked by maintenance agents + on-demand by the operator.
- **`tests/`** — pytest unit tests. Run in CI, not against live state.
