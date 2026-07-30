"""Pipeline-tagged check: the tier-1 acting-set ingest actually ran, and
covered enough of the acting universe to be worth trusting.

Three-tier ingestion (operator directive 2026-07-29) says no ACTING strategy
may decide on the previous day's EOD collect. The 14:30 ET tier-1 job
(scripts/run_acting_ingest.py) is what makes that true, and the engine
deliberately falls OPEN when its overlay is thin or missing — a partial
overlay beats a no-trade day.

That fail-open is exactly why this check exists. Without it, a tier-1 job that
silently fetched nothing is indistinguishable from one that never needed to
run: the cycle still trades, on yesterday's surface, reporting success. "Asked
and absent" must never look like "couldn't ask" (feedback_silent_failure_pattern).

Reports, in order of severity:
  FAIL  — manifest missing on a weekday after the ingest slot, or an adapter
          category errored, or coverage collapsed
  WARN  — coverage below the floor, or acting categories with no adapter yet
  PASS  — every adapter-backed category covered above the floor

Deliberately silent before the ingest slot and on weekends: the job has not
run yet, which is not a defect.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..registry import check
from ..types import Status

_ROOT = Path(os.environ.get('OPENCLAW_ROOT', '/root/openclaw'))
_INTRADAY = _ROOT / 'data' / 'derived' / 'intraday'
_ET = ZoneInfo('America/New_York')
# The 14:30 ET job needs ~6 min for the full universe; give it until 15:00,
# when the compute reads the overlay and the answer starts to matter.
_DUE_HOUR_ET = int(os.environ.get('ACTING_INGEST_DUE_HOUR_ET', '15'))
# Measured 2026-07-30: 4,234 of 5,056 requested tickers returned a chain
# (817 have no listed options at all), so ~84% is a healthy full run.
_MIN_COVERAGE = float(os.environ.get('ACTING_INGEST_MIN_COVERAGE', '0.60'))


def _is_due(now: datetime) -> bool:
    return now.weekday() < 5 and now.hour >= _DUE_HOUR_ET


def _grade(cat: str, adapter: str, res: dict) -> tuple:
    """(verdict, detail) per adapter.

    Graded on whether the FETCH SUCCEEDED, never on how many rows came back.
    options_eod is a per-ticker sweep where a low hit rate is a real problem;
    insider and financials are EVENT STREAMS where zero new rows is an ordinary
    quiet day. Grading those on row count would WARN most days, and a check
    that cries wolf is worse than no check.
    """
    elapsed = res.get('elapsed_s', '?')
    if adapter == 'intraday_options':
        requested = res.get('requested') or 0
        if not requested:
            return 'ok', f'{cat}: nothing requested'
        # Symbol classes with no listed options were never askable; grading
        # against them would make a healthy run look like a partial one.
        askable = requested - (res.get('skipped_class') or 0)
        covered = res.get('ok') or 0
        cov = covered / askable if askable else 0.0
        detail = (f'{cat}: {covered}/{askable} ({cov:.0%}), '
                  f'{res.get("rows", 0)} rows, {elapsed}s')
        if res.get('skipped_budget'):
            return 'warn', (f'{detail} — {res["skipped_budget"]} tickers never '
                            f'requested (wall-clock budget expired)')
        if cov < _MIN_COVERAGE:
            return 'warn', f'{detail} — below the {_MIN_COVERAGE:.0%} floor'
        return 'ok', detail

    if adapter == 'intraday_insider':
        pages = res.get('pages') or 0
        detail = (f'{cat}: {res.get("rows", 0)} new filing(s) from {pages} page(s), '
                  f'{res.get("dup_in_master", 0)} already held, {elapsed}s')
        if pages == 0:
            return 'fail', f'{detail} — the filing stream returned nothing at all'
        if res.get('budget_expired'):
            return 'warn', f'{detail} — stream truncated by the wall-clock budget'
        if res.get('http_errors'):
            return 'warn', f'{detail} — {res["http_errors"]} page fetch error(s)'
        return 'ok', detail

    if adapter == 'intraday_financials':
        reporters = res.get('reporters') or 0
        fetched = res.get('fetched') or 0
        detail = (f'{cat}: {res.get("rows", 0)} new period-row(s) from '
                  f'{fetched}/{reporters} reporter(s), {elapsed}s')
        if reporters == 0:
            return 'ok', f'{cat}: no in-universe reporters today (quiet day)'
        if res.get('skipped_budget'):
            return 'warn', (f'{detail} — {res["skipped_budget"]} reporter(s) '
                            f'never fetched (wall-clock budget expired)')
        if fetched / reporters < _MIN_COVERAGE:
            return 'warn', f'{detail} — below the {_MIN_COVERAGE:.0%} fetch floor'
        return 'ok', detail

    return 'warn', f'{cat}: unrecognized adapter {adapter!r}'


@check(name='acting_ingest_coverage', tags=['pipeline'], requires=[])
def _acting_ingest_coverage():
    if os.environ.get('OPENCLAW_SAMEDAY_EXEC') != '1':
        return Status.PASS, 'same-day execution off — tier-1 ingest not scheduled'

    now = datetime.now(_ET)
    manifest_path = _INTRADAY / date.today().isoformat() / 'manifest.json'
    if not manifest_path.exists():
        if not _is_due(now):
            return Status.PASS, (
                f'tier-1 ingest not due yet (runs 14:30 ET, graded from '
                f'{_DUE_HOUR_ET}:00 ET)')
        return Status.FAIL, (
            f'no tier-1 manifest for {date.today()} — the 14:30 ET acting-set '
            f'ingest did not run, so every acting strategy decided on the '
            f'previous EOD collect (engine falls open silently by design)')
    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception as e:  # noqa: BLE001
        return Status.FAIL, f'tier-1 manifest unreadable: {e}'

    fails: list[str] = []
    warns: list[str] = []
    oks: list[str] = []
    for cat, res in sorted((manifest.get('categories') or {}).items()):
        adapter = res.get('adapter')
        if res.get('error'):
            fails.append(f'{cat}: {res["error"]}')
            continue
        if adapter in (None, 'none'):
            warns.append(f'{cat}: no intraday adapter ({len(res.get("consumers") or [])} '
                         f'consumers on the last EOD collect)')
            continue
        if adapter == 'skipped' or res.get('dry_run'):
            continue
        verdict, detail = _grade(cat, adapter, res)
        cov = res.get('master_ticker_coverage')
        if cov and cov.get('frac', 1.0) < 1.0:
            # Freshness and coverage are different failures. Saying so keeps
            # "adapter live" from reading as "category covered".
            detail += (f' [master covers {cov["in_master"]}/{cov["wanted"]} '
                       f'of the acting universe]')
        (fails if verdict == 'fail' else warns if verdict == 'warn'
         else oks).append(detail)

    if fails:
        return Status.FAIL, '; '.join(fails + warns)
    if warns:
        return Status.WARN, '; '.join(warns + oks)
    if not oks:
        return Status.WARN, 'tier-1 manifest has no adapter-backed category'
    return Status.PASS, '; '.join(oks)
