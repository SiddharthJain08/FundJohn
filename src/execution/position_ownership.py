"""position_ownership.py — per-ticker broker-vs-ledger ownership.

Stream B item 15 (docs/specs/2026-09-12-quantdinger-adoptions-spec.md:167-181).

Nightly, in the pipeline's `reconcile` step: for each ticker compare the
broker's signed share count against what the open execution_signals rows claim,
classify ok / unallocated / shortfall, upsert one row per (cycle_date, ticker),
and log ONLY on a status transition.

Enforcement is opt-in. With OPENCLAW_OWNERSHIP_BLOCK=1 the sizer sheds OPENS and
ADDS on tickers whose latest status is not ok. Unset (the default) is report-only
and byte-identical to today's behaviour. Exits and flattens are NEVER blocked —
the sizer applies the blocklist through the same `_shed` helper the stop-out
cooldown uses (regime_blended_sizer._apply_entry_hygiene_gate:2398-2404).

signal_qty: execution_signals carries no share column, so the count comes from
alpaca_submissions.filled_qty joined by (run_date -> target_date, ticker,
strategy_id) — the key parity_mark.backfill_broker_fill_truth:323-332 uses.
filled_qty is the ENTRY quantity and never decreases, so it is netted by the
exit fills in broker_fills (migration 155): any fill on that ticker after the
earliest entry submission whose order_id is NOT itself a recorded entry
submission is an exit, applied ONCE per ticker. Before broker_fills has a day of
history a partially-exited position reads `shortfall` — which is why the default
is report-only.

DEFERRED (fix round 1, item 5, ruled to the wave-2 final fix wave): the exit-net
subquery keyed on `bf.filled_at >= <earliest entry submission>` can subtract the
exit fill of a PRIOR, already-closed position on the same ticker (a round-trip
that finished before today's open signal was even submitted) — needs
fill-to-submission attribution by order id, not a ticker+timestamp band. Not
fixed here.

Crypto tickers (BASE-USD convention, e.g. 'BTC-USD') are excluded from both
sides of the comparison (fix round 1, item 3): stop_reattach.fetch_positions is
equity-only so they never appear in account_qty, but DO appear in signal_qty via
execution_signals — left unguarded that reads as a permanent, un-fixable
shortfall on every crypto entry.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

EXTRA_TOL_FRAC = 0.001      # broker may hold 0.1% more than the ledger claims
SHORTFALL_TOL_FRAC = 0.005  # ledger may claim 0.5% more than the broker holds
DUST_SHARES = 1.0           # absolute floor: 1 share is never a finding

STATUS_OK = 'ok'
STATUS_UNALLOCATED = 'unallocated'
STATUS_SHORTFALL = 'shortfall'

_LONG_DIRECTIONS = {'LONG', 'BUY', 'BUY_VOL'}
_SHORT_DIRECTIONS = {'SHORT', 'SELL', 'SELL_VOL'}


def classify(account_qty, signal_qty, *,
             extra_tol: float = EXTRA_TOL_FRAC,
             shortfall_tol: float = SHORTFALL_TOL_FRAC,
             dust: float = DUST_SHARES):
    """(unknown_qty, status) for one ticker.

    unknown = account - signal on SIGNED quantities, so a short book reads the
    same way a long one does. unknown > 0 means the broker holds exposure no
    open signal claims (unallocated); unknown < 0 means open signals claim
    exposure the broker does not hold (shortfall) — the dangerous direction,
    which is why it gets the looser band before we cry wolf. Both are floored at
    `dust` shares so rounding residue is never a finding."""
    a = float(account_qty)
    s = float(signal_qty)
    unknown = a - s
    scale = max(abs(a), abs(s))
    if unknown > 0:
        tol = max(dust, extra_tol * scale)
        return unknown, (STATUS_OK if unknown <= tol else STATUS_UNALLOCATED)
    if unknown < 0:
        tol = max(dust, shortfall_tol * scale)
        return unknown, (STATUS_OK if -unknown <= tol else STATUS_SHORTFALL)
    return 0.0, STATUS_OK


def compute_ownership(account_qty: dict, signal_qty: dict, **kw) -> list:
    """[{ticker, account_qty, signal_qty, unknown_qty, status}] over the UNION of
    both key sets, sorted by ticker. A ticker present on only one side gets 0.0
    on the other — exactly the case this ledger exists to catch."""
    rows = []
    for tkr in sorted(set(account_qty or {}) | set(signal_qty or {})):
        a = float((account_qty or {}).get(tkr) or 0.0)
        s = float((signal_qty or {}).get(tkr) or 0.0)
        unknown, status = classify(a, s, **kw)
        rows.append({'ticker': tkr, 'account_qty': a, 'signal_qty': s,
                     'unknown_qty': round(unknown, 6), 'status': status})
    return rows


def transitions(rows, previous: dict) -> list:
    """One line per ticker whose status CHANGED vs `previous` ({ticker: status}
    from the last cycle). A ticker with no prior row counts as a transition only
    when its status is not ok — the first sighting of a healthy name is not news
    and would flood the log on the very first pass."""
    previous = previous or {}
    out = []
    for r in rows:
        prev = previous.get(r['ticker'])
        if prev == r['status']:
            continue
        if prev is None and r['status'] == STATUS_OK:
            continue
        out.append(f"[ownership] {r['ticker']}: {prev or 'new'} -> {r['status']} "
                   f"(account={r['account_qty']:g} signal={r['signal_qty']:g} "
                   f"unknown={r['unknown_qty']:+g})")
    return out


def load_account_qty(fetch_positions=None):
    """{ticker: signed share qty} from the broker, or None when the CLI call
    FAILED. stop_reattach.fetch_positions returns None for "couldn't ask" vs []
    for a genuinely flat book (:234-236); the ownership pass MUST skip on None —
    classifying an unreadable book would mark every ticker shortfall."""
    if fetch_positions is None:
        from execution.stop_reattach import fetch_positions as _fp
        fetch_positions = _fp
    positions = fetch_positions()
    if positions is None:
        return None
    out: dict = {}
    for p in positions:
        sym = p.get('symbol')
        if not sym:
            continue
        try:
            out[sym] = out.get(sym, 0.0) + float(p.get('qty') or 0.0)
        except (TypeError, ValueError):
            continue
    return out


def _is_crypto_ticker(ticker) -> bool:
    """Delegates to alpaca_executor._is_crypto_ticker (BASE-USD convention,
    e.g. 'BTC-USD') — imported lazily so this module (loaded by the reconcile
    step on every cycle) doesn't pay alpaca_executor's import cost unless a
    crypto ticker is actually present that day (fix round 1, item 3)."""
    from execution.alpaca_executor import _is_crypto_ticker as _impl
    return _impl(ticker)


def _exclude_crypto(qty: dict):
    """(filtered_dict, {crypto tickers removed}). Crypto never has a broker-side
    qty (stop_reattach.fetch_positions is equity-only) but DOES appear in
    signal_qty via execution_signals — excluded from both sides here so a
    crypto entry never reads as a permanent, un-fixable shortfall (fix round 1,
    item 3)."""
    qty = qty or {}
    crypto = {t for t in qty if _is_crypto_ticker(t)}
    if not crypto:
        return qty, crypto
    return {t: v for t, v in qty.items() if t not in crypto}, crypto


# DISTINCT ON (per submission) is defense in depth for the real query plan;
# the seen-set dedupe in load_signal_qty (below) is the tested guarantee — a
# fake DB cursor can't exercise Postgres' own DISTINCT ON. Same pattern as
# fill_slippage._ENTRY_SQL / _dedupe_by_submission (fix round 1, item 4).
_MATCHED_ENTRY_SQL = """
    SELECT DISTINCT ON (s.run_date, s.strategy_id, s.ticker)
           s.run_date, s.strategy_id, es.ticker, es.direction,
           COALESCE(s.filled_qty, 0) AS filled_qty, s.submitted_at
      FROM execution_signals es
      JOIN alpaca_submissions s ON s.run_date = es.target_date
                               AND s.ticker = es.ticker
                               AND s.strategy_id = es.strategy_id
     WHERE es.status = 'open'
       AND (es.lifecycle_state IS NULL OR es.lifecycle_state = 'FILLED')
       AND es.ticker IS NOT NULL
     ORDER BY s.run_date, s.strategy_id, s.ticker, es.id
"""

# Per-ticker exit-fill net since the earliest (valid-direction) entry
# submission on that ticker. DEFERRED limitation (item 5, see module
# docstring): `filled_at >= since` can also net the exit fill of a prior,
# already-closed position on the same ticker.
_EXIT_NET_SQL = """
    SELECT COALESCE(SUM(CASE WHEN LOWER(bf.side) = 'sell' THEN -bf.qty ELSE bf.qty END), 0)
      FROM broker_fills bf
     WHERE bf.ticker = %s
       AND bf.filled_at >= %s
       AND NOT EXISTS (SELECT 1 FROM alpaca_submissions s2
                        WHERE s2.alpaca_order_id = bf.order_id)
"""


def load_signal_qty(cur, log_fn=None) -> dict:
    """{ticker: signed share qty the open signals claim}.

    Two execution_signals rows can share ONE physical submission —
    execution_signals is unique on (strategy_id, signal_date, ticker,
    direction), NOT target_date, so an overnight signal and a same-day signal
    (or a long+short flip) for the same strategy/ticker/target_date both join
    the same alpaca_submissions row. `_MATCHED_ENTRY_SQL`'s DISTINCT ON keys
    off (run_date, strategy_id, ticker) with a deterministic ORDER BY (...,
    es.id) tiebreak; the seen-set below re-applies the identical collapse in
    Python so that one physical submission's filled_qty is counted exactly
    ONCE regardless of how many execution_signals rows point at it (fix round
    1, item 4).

    A row whose direction is neither a known long nor a known short marker is
    SKIPPED (not folded into short) and logged once per pass (fix round 1,
    item 6) — mapping an unknown/NULL direction to short would silently
    misclassify an unrelated or malformed signal as a short position.

    Entry fills are netted by every non-entry fill on that ticker since the
    earliest valid-direction entry submission — applied ONCE per ticker, never
    once per signal (two strategies on the same name would otherwise
    double-count the exit)."""
    emit = log_fn or logger.info
    cur.execute(_MATCHED_ENTRY_SQL)
    seen = set()
    entry_qty: dict = {}
    first_submitted_at: dict = {}
    n_unknown = 0
    for r in (cur.fetchall() or []):
        if not r or not r[2]:
            continue
        run_date, strategy_id, ticker, direction, filled_qty, submitted_at = r
        key = (run_date, strategy_id, ticker)
        if key in seen:
            continue
        seen.add(key)
        d = (direction or '').strip().upper()
        if d in _LONG_DIRECTIONS:
            sign = 1.0
        elif d in _SHORT_DIRECTIONS:
            sign = -1.0
        else:
            n_unknown += 1
            continue
        entry_qty[ticker] = entry_qty.get(ticker, 0.0) + sign * float(filled_qty or 0.0)
        prev = first_submitted_at.get(ticker)
        if submitted_at is not None and (prev is None or submitted_at < prev):
            first_submitted_at[ticker] = submitted_at
    if n_unknown:
        emit(f'[ownership] {n_unknown} signal row(s) skipped (unknown/NULL direction)')

    out: dict = {}
    for ticker, qty in entry_qty.items():
        since = first_submitted_at.get(ticker)
        net = 0.0
        if since is not None:
            cur.execute(_EXIT_NET_SQL, (ticker, since))
            net_rows = cur.fetchall() or []
            if net_rows and net_rows[0] is not None:
                net = float(net_rows[0][0] or 0.0)
        out[ticker] = qty + net
    return out


def load_previous_statuses(cur, cycle_date) -> dict:
    """{ticker: status} at the most recent cycle_date STRICTLY BEFORE this one —
    the baseline `transitions` diffs against."""
    cur.execute(
        'SELECT ticker, status FROM position_ownership WHERE cycle_date = '
        '(SELECT MAX(cycle_date) FROM position_ownership WHERE cycle_date < %s)',
        (cycle_date,))
    return {r[0]: r[1] for r in (cur.fetchall() or []) if r and r[0]}


def persist_ownership(cur, cycle_date, rows) -> int:
    """Upsert one row per (cycle_date, ticker): DO UPDATE, not DO NOTHING (fix
    round 1, item 2 — ruled). position_ownership is a DERIVED ledger, not a
    canonical table (it is not in the CLAUDE.md append-only-master list): a
    second reconcile run on the same cycle_date (reconcile runs after
    `alpaca` and again on every intraday redeploy) must overwrite with the
    LATEST snapshot, since Task 8's blocklist and the position_ownership_clean
    check read the newest cycle_date and the FIRST same-day snapshot is the
    noisiest. `created_at` (first-seen) is never touched by the UPDATE branch
    — only INSERT's DEFAULT now() sets it. Transitions still diff against the
    previous CYCLE_DATE (load_previous_statuses), unaffected by a same-day
    correction. Returns the cursor's summed rowcount (rows actually written),
    not len(rows)."""
    n = 0
    for r in rows:
        cur.execute(
            'INSERT INTO position_ownership '
            '(cycle_date, ticker, account_qty, signal_qty, unknown_qty, status) '
            'VALUES (%s,%s,%s,%s,%s,%s) '
            'ON CONFLICT (cycle_date, ticker) DO UPDATE SET '
            'account_qty = EXCLUDED.account_qty, '
            'signal_qty = EXCLUDED.signal_qty, '
            'unknown_qty = EXCLUDED.unknown_qty, '
            'status = EXCLUDED.status',
            (cycle_date, r['ticker'], r['account_qty'], r['signal_qty'],
             r['unknown_qty'], r['status']))
        n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return n


def latest_status_map(cur) -> dict:
    """{ticker: status} at the newest cycle_date — what the sizer blocklist and
    the position_ownership_clean system check read."""
    cur.execute(
        'SELECT ticker, status FROM position_ownership WHERE cycle_date = '
        '(SELECT MAX(cycle_date) FROM position_ownership)')
    return {r[0]: r[1] for r in (cur.fetchall() or []) if r and r[0]}


def run_ownership_pass(conn, cycle_date, *, dry_run: bool = False,
                       account_qty=None, log_fn=None) -> dict:
    """The reconcile-step hook. Returns
    {'rows', 'unallocated', 'shortfall', 'transitions', 'crypto_skipped'} or
    {'skipped': reason}.

    NEVER raises: an ownership finding must not fail the reconcile step, whose
    critical path is the submission reconcile. `conn` may already have other
    work committed on it (reconcile() / cleanup_phantom_signals each own their
    own commit) — this pass wraps its DB work (cursor open, SAVEPOINT,
    inserts, and the eventual commit) in its own SAVEPOINT, mirroring
    sp_broker_fills / sp_filled_at / sp_exit_slip, so an UNAPPLIED migration
    156 (position_ownership missing), or any other psycopg2 error, rolls back
    cleanly instead of leaving the connection InFailedSqlTransaction — or
    raising past this function — for whatever runs after it (fix round 1,
    item 1, CRITICAL: `cur = conn.cursor()`, the SAVEPOINT, and `conn.commit()`
    now all live inside the try; a failure of the ROLLBACK TO SAVEPOINT /
    RELEASE itself — a double fault — is swallowed rather than allowed to mask
    the original error or escape this function)."""
    emit = log_fn or logger.info
    try:
        if account_qty is None:
            account_qty = load_account_qty()
    except Exception as e:  # noqa: BLE001
        emit(f'[ownership] pass failed ({type(e).__name__}: {e}) — skipped')
        return {'skipped': f'{type(e).__name__}: {e}'}
    if account_qty is None:
        emit('[ownership] broker position list unavailable — pass SKIPPED')
        return {'skipped': 'broker_unavailable'}

    try:
        # _exclude_crypto lazily imports alpaca_executor (psycopg2 +
        # alpaca_trader + handoff + regime_liquidator) — an import failure
        # there must land in the SAME skip path as everything else in this
        # try, not raise past run_ownership_pass (fix round 1, item 1 applied
        # to the item-3 crypto-exclusion path added alongside it).
        account_qty, crypto_a = _exclude_crypto(account_qty)
        cur = conn.cursor()
        try:
            cur.execute('SAVEPOINT sp_ownership')
            signal_qty = load_signal_qty(cur, log_fn=emit)
            signal_qty, crypto_s = _exclude_crypto(signal_qty)
            crypto_skipped = len(crypto_a | crypto_s)
            rows = compute_ownership(account_qty, signal_qty)
            lines = transitions(rows, load_previous_statuses(cur, cycle_date))
            if not dry_run:
                persist_ownership(cur, cycle_date, rows)
            cur.execute('RELEASE SAVEPOINT sp_ownership')
            if not dry_run:
                conn.commit()
        except Exception:
            try:
                cur.execute('ROLLBACK TO SAVEPOINT sp_ownership')
                cur.execute('RELEASE SAVEPOINT sp_ownership')
            except Exception:  # noqa: BLE001 — a double fault here must never mask the original error
                pass
            raise
        finally:
            cur.close()
    except Exception as e:  # noqa: BLE001
        emit(f'[ownership] pass failed ({type(e).__name__}: {e}) — skipped')
        return {'skipped': f'{type(e).__name__}: {e}'}

    for line in lines:
        emit(line)
    stats = {
        'rows': len(rows),
        'unallocated': sum(1 for r in rows if r['status'] == STATUS_UNALLOCATED),
        'shortfall': sum(1 for r in rows if r['status'] == STATUS_SHORTFALL),
        'transitions': lines,
        'crypto_skipped': crypto_skipped,
    }
    emit(f"[ownership] {stats['rows']} ticker(s): {stats['unallocated']} unallocated, "
         f"{stats['shortfall']} shortfall, {len(lines)} transition(s), "
         f"crypto_skipped={crypto_skipped}")
    return stats
