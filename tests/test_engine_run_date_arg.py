"""engine.py --date argument — the orchestrator has ALWAYS passed it.

resolve_script.js / pipeline_orchestrator.py append `--date <runDate>` to every
src/execution step invocation, but engine.main() ignored argv and used
date.today(). Harmless while the two coincide; wrong for any historical re-run
(e.g. recomputing an EOD target set after a data fix — 2026-06-04 §10).
"""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / 'src')):
    if p not in sys.path:
        sys.path.insert(0, p)

from execution import engine  # noqa: E402


def test_parse_run_date_explicit():
    assert engine._parse_run_date(['--date', '2026-06-03']) == date(2026, 6, 3)


def test_parse_run_date_defaults_to_today():
    assert engine._parse_run_date([]) == date.today()


def test_parse_run_date_tolerates_unknown_flags():
    # PIPELINE_DRY_RUN=1 appends --dry-run to every step argv; the engine
    # must not crash on flags it does not (yet) implement.
    assert engine._parse_run_date(['--date', '2026-06-03', '--dry-run']) == date(2026, 6, 3)


def test_parse_run_date_rejects_garbage():
    import pytest
    with pytest.raises(SystemExit):
        engine._parse_run_date(['--date', 'not-a-date'])
