"""SP-7 Phase D1: options_eligible producer.

Weekly bulk enumeration of optionable underlyings via the Alpaca options
*contracts* reference endpoint (`option contracts --status active`). Writes
data/.cache/options_eligibility.json ({symbol: True} for eligible names),
which src/pipeline/run_ticker_metadata_step.py reads to set the
ticker_metadata_snapshots.options_eligible column.

Safety: a COMPLETE + plausibly-sized sweep full-replaces the cache; any
failure (partial sweep, API outage, degenerate result) keeps the prior
cache, so eligibility is never silently wiped. Inert to land — no live
strategy reads an options predicate and the resolver/archive gates are OFF.

Spec: docs/superpowers/specs/2026-06-08-sp7-phase-d1-options-eligible-producer-design.md
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

ALPACA_BIN = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')
CACHE_PATH = Path(os.environ.get(
    'OPTIONS_ELIGIBILITY_CACHE',
    '/root/openclaw/data/.cache/options_eligibility.json'))
PAGE_LIMIT = int(os.environ.get('OPTIONS_ELIGIBILITY_PAGE_LIMIT', '10000'))
SOFT_BUDGET_S = int(os.environ.get('OPTIONS_ELIGIBILITY_BUDGET_S', '1800'))
ABS_FLOOR = int(os.environ.get('OPTIONS_ELIGIBILITY_MIN_FLOOR', '1000'))
WEBHOOK_URL = os.environ.get('OPENCLAW_OPTIONS_ELIGIBILITY_WEBHOOK', '')


def _record_call(success: bool, error: str | None = None) -> None:
    """Best-effort provider_health hook (mirrors backfillers/alpaca_options.py)."""
    try:
        from src.maintenance.provider_health import record
        record('alpaca', 'options_contracts', success=success, error=error)
    except Exception:
        pass


def _fetch_contracts_page(page_token: str | None = None, limit: int = PAGE_LIMIT) -> dict:
    """One page of `alpaca option contracts --status active`. Raises on failure."""
    args = [ALPACA_BIN, 'option', 'contracts', '--status', 'active',
            '--limit', str(limit)]
    if page_token:
        args.extend(['--page-token', page_token])
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except Exception as e:
        _record_call(False, f'subprocess: {e}')
        raise
    if res.returncode != 0:
        _record_call(False, f'rc={res.returncode}: {res.stderr.strip()[:160]}')
        raise RuntimeError(f'alpaca option contracts rc={res.returncode}: {res.stderr.strip()}')
    page = json.loads(res.stdout)
    if page.get('error'):            # rc=0 error envelope -> treat as failure
        _record_call(False, str(page.get('error'))[:160])
        raise RuntimeError(f'alpaca option contracts envelope error: {page.get("error")}')
    _record_call(True)
    return page


def _parse_underlyings(page: dict) -> set[str]:
    return {c.get('underlying_symbol')
            for c in (page.get('option_contracts') or [])
            if c.get('underlying_symbol')}


def enumerate_optionable_underlyings(fetch_page=_fetch_contracts_page,
                                     budget_s: int = SOFT_BUDGET_S,
                                     clock=time.time):
    """Page the active option-contracts list; collect distinct underlying_symbol.

    Returns (optionable: set[str], completed: bool, pages: int). completed is
    True ONLY when a page returns no next_page_token (a genuinely terminal
    sweep). Budget exceeded or any page error → completed=False (caller keeps
    the prior cache).
    """
    optionable: set[str] = set()
    token = None
    deadline = clock() + budget_s
    pages = 0
    seen: set[str] = set()
    while True:
        if clock() > deadline:
            log.warning('sweep budget exceeded after %d pages', pages)
            return optionable, False, pages
        try:
            page = fetch_page(token)
        except Exception as e:  # noqa: BLE001 — any error aborts the sweep safely
            log.warning('sweep page %d failed: %s', pages, e)
            return optionable, False, pages
        optionable |= _parse_underlyings(page)
        pages += 1
        token = page.get('next_page_token')
        if not token:
            return optionable, True, pages
        if token in seen:
            log.warning('repeating page token %r — aborting sweep', token)
            return optionable, False, pages
        seen.add(token)


def _load_universe() -> set[str]:
    """Active US-equity symbols from alpaca_tradable_universe (the metadata
    universe; all 13.8k active rows are us_equity as of 2026-06-08)."""
    import psycopg2
    with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
        cur.execute("SELECT symbol FROM alpaca_tradable_universe "
                    "WHERE status='active' AND asset_class='us_equity'")
        return {r[0] for r in cur.fetchall()}


def _load_prior_cache(path=CACHE_PATH) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _atomic_write_cache(data: dict, path=CACHE_PATH) -> None:
    """Write via temp file + os.replace so a crash never leaves a partial cache."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w') as f:
        json.dump(data, f, sort_keys=True)
    os.replace(tmp, path)


def build_eligibility(optionable: set[str], universe: set[str]) -> dict[str, bool]:
    """Full-replace snapshot: {sym: True} for the optionable subset of our
    universe (absent ⇒ False at read time). A name that lost its listing simply
    drops out of the fresh snapshot."""
    return {sym: True for sym in sorted(optionable & universe)}


def decide_write(new: dict, prior: dict, completed: bool, abs_floor: int = ABS_FLOOR):
    """Return (should_write, reason). Write only on a complete sweep whose
    eligible count clears both the absolute floor and 50% of the prior count."""
    if not completed:
        return False, 'incomplete sweep — prior cache retained'
    n_new = sum(1 for v in new.values() if v)
    prior_n = sum(1 for v in prior.values() if v)
    floor = max(abs_floor, prior_n // 2)
    if n_new < floor:
        return False, f'sanity floor: {n_new} < {floor} (abs={abs_floor}, prior={prior_n})'
    return True, f'ok: {n_new} eligible'


def _format_summary(stats: dict) -> str:
    return ('**options_eligible refresh** — '
            f"eligible={stats['eligible']} / universe={stats['universe']} · "
            f"pages={stats['pages']} · +{stats['added']}/-{stats['removed']} · "
            f"{stats['action']} · {stats['secs']:.0f}s")


def _post_summary(text: str, webhook_url: str = WEBHOOK_URL) -> None:
    """Best-effort Discord post. UA header is REQUIRED — Cloudflare 1010-bans the
    default python-urllib UA (reference_discord_urllib_cloudflare_ua)."""
    if not webhook_url or not text:
        return
    import urllib.request
    req = urllib.request.Request(
        webhook_url, data=json.dumps({'content': text}).encode(),
        headers={'Content-Type': 'application/json',
                 'User-Agent': 'OpenClaw-OptionsEligibility/1.0 (+botjohn)'})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # noqa: BLE001 — never fail the producer over Discord
        log.warning('discord post failed: %s', e)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='SP-7 D1 options_eligible producer')
    ap.add_argument('--dry-run', action='store_true',
                    help='write to /tmp and skip the Discord post')
    ap.add_argument('--limit', type=int, default=PAGE_LIMIT)
    ap.add_argument('--budget-s', type=int, default=SOFT_BUDGET_S)
    args = ap.parse_args(argv)

    cache_path = Path('/tmp/options_eligibility_dryrun.json') if args.dry_run else CACHE_PATH
    t0 = time.time()
    optionable, completed, pages = enumerate_optionable_underlyings(
        fetch_page=lambda tok: _fetch_contracts_page(tok, args.limit),
        budget_s=args.budget_s)
    universe = _load_universe()
    prior = _load_prior_cache(cache_path)
    new = build_eligibility(optionable, universe)
    should, reason = decide_write(new, prior, completed)

    prior_keys = {k for k, v in prior.items() if v}
    stats = {
        'eligible': len(new), 'universe': len(universe), 'pages': pages,
        'added': len(set(new) - prior_keys), 'removed': len(prior_keys - set(new)),
        'secs': time.time() - t0,
        'action': 'WROTE' if should else f'KEPT-PRIOR ({reason})',
    }
    if should:
        _atomic_write_cache(new, cache_path)
    summary = _format_summary(stats)
    log.info('options-eligibility %s', summary)
    if not args.dry_run:
        _post_summary(summary)
    return 0 if should else 1


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    raise SystemExit(main())
