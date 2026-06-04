"""Regime-blended sizer.

Pipeline orchestrator's `trade` step calls this via regime_blended_sizer_live
for production submission. Single path: sharpe_cadence — the per-(strategy,
regime) daily_weight × direction aggregator with broker-position netting.

Legacy mode dispatch (consolidate / independent paths) and the
OPENCLAW_SHARPE_CADENCE_SIZER feature flag were removed 2026-05-21 once
sharpe_cadence had been LIVE since 2026-05-12 and validated through
multiple production cycles. The historical paths and the ticker_consolidator
module they used live in git history only.
"""
from __future__ import annotations
import json
import logging
import os
import re
from datetime import date

from execution.signal_cadence_gate import filter_by_cadence, advance_last_fire
from execution.tradejohn_confirmer import confirm as default_confirmer
from execution.alpaca_executor import _spot_price

logger = logging.getLogger(__name__)

# SP-6 per-ticker conviction cap fraction (operator formula 2026-06-04):
# cap_usd(ticker) = PER_TICKER_CAP_SHARPE_FRAC × |gate_net_sharpe| × λ × NAV.
# Applied in EOD mode only (OPENCLAW_EOD_RECONCILE=1) inside
# _sharpe_cadence_path — see the cap block there for full rationale.
PER_TICKER_CAP_SHARPE_FRAC = 0.05

# SP-5.3 — force-close held option structures at/below this calendar DTE
# (operator-locked 2026-06-04: 7 calendar days, single rule for all structures).
# Override via OPENCLAW_OPTION_EXPIRY_CLOSE_DTE (parse-guarded).
OPTION_EXPIRY_CLOSE_DTE = 7


def _ortho_enabled(gate: str) -> bool:
    return os.environ.get(gate) == '1'


def size_positions(
    signals: list[dict],
    account_state: dict,
    regime: dict,
    run_date: date,
    strategy_state: dict,
    regime_params: dict,
    confirmer=None,
) -> list[dict]:
    """Returns list of sized orders for the cycle.

    Pipeline:
      1. Cadence gate (filter_by_cadence) — drops signals from strategies
         whose next_fire_date is still in the future. Bypassed for one
         cycle when regime_liquidator sets regime:transition:fresh in Redis.
         **Bypassed entirely in EOD mode** (OPENCLAW_EOD_RECONCILE=1): the
         APPROVED carried set was already gated at 4PM[T] + the 9:15
         pre-market gate, so re-applying cadence would wrongly skip them.
      2. Sharpe-cadence path — pulls active-window signals from the DB,
         aggregates ticker_w across contributing strategies, normalizes
         to λ × NAV, delta-rebalances against current broker positions,
         and routes flip cases through paired close/open emissions.

    Caller (regime_blended_sizer_live) handles persistence to
    execution_signals and the sized handoff downstream.
    """
    regime_state = regime['state']

    if os.environ.get('OPENCLAW_EOD_RECONCILE') == '1':
        # SP-6 Phase A — EOD reconcile mode. The APPROVED carried set was
        # already gated at the 4PM[T] EOD compute and the 9:15 pre-market
        # carry-forward gate; re-applying cadence here would wrongly skip
        # signals that are pre-approved for T+1 execution. Signal loading
        # also switches to _load_approved_carried_signals (see
        # _sharpe_cadence_path). Note: _check_force_fire_flag() is NOT
        # called in EOD mode — the Redis regime:transition:fresh key must
        # not be consumed mid-reconcile.
        passed = list(signals)
        logger.info('regime_blended_sizer: EOD mode — cadence gate bypassed, %d carried signals', len(passed))
    else:
        # Legacy path — untouched. Cadence gate guards against stacking on
        # long-horizon strategies. Bypassed for one cycle on regime transition.
        force_all = _check_force_fire_flag()
        passed, skipped = filter_by_cadence(signals, strategy_state, run_date, force_all=force_all)
        if skipped:
            logger.info('regime_blended_sizer: cadence skipped %d signals', len(skipped))
        if force_all:
            logger.info('regime_blended_sizer: force_all=True — cadence bypassed (regime transition)')

        if not passed:
            return []

    return _sharpe_cadence_path(passed, account_state, regime_state,
                                regime_params, confirmer or default_confirmer)


def _check_force_fire_flag() -> bool:
    """Consume the regime:transition:fresh Redis key. Returns True once
    per regime transition; subsequent calls return False until the next
    liquidation re-sets it."""
    try:
        import redis as _redis
        r = _redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'),
                            socket_connect_timeout=3, decode_responses=True)
        if r.get('regime:transition:fresh'):
            r.delete('regime:transition:fresh')
            return True
    except Exception:
        pass
    return False


def _dir_to_int(d) -> int:
    """Normalize a signal direction to +1 (long-side) or -1 (short-side).
    Handles all six raw values + integer / float / 'long' / 'short' forms.
    Returns 0 if unparseable — such signals are excluded from sizing.
    """
    if d is None:
        return 0
    if isinstance(d, (int, float)):
        return 1 if d > 0 else (-1 if d < 0 else 0)
    u = str(d).strip().upper()
    if u in ('LONG', 'BUY', 'BUY_VOL', '1', '+1'):
        return 1
    if u in ('SHORT', 'SELL', 'SELL_VOL', '-1'):
        return -1
    return 0


def _derive_action(kind: str, out_current: float, out_target: float, dir_sign: int) -> str:
    """Human-readable trade action, derived from the order's emission kind and
    the current/target position USD. The wire field `direction` (long/short)
    stays the order SIDE (buy/sell) the executor reads; `action` is for humans
    (so a long-*reduction* reads `reduce_long`, not `short`)."""
    cur = out_current or 0.0
    tgt = out_target or 0.0
    if kind in ('orphan_close', 'flip_close'):
        return 'close_long' if cur > 0 else 'close_short'
    if kind == 'flip_open':
        return 'flip_to_long' if tgt > 0 else 'flip_to_short'
    # delta
    if cur == 0.0:
        return 'open_long' if tgt > 0 else 'open_short'
    if (cur > 0) == (tgt > 0):                      # same side
        side = 'long' if cur > 0 else 'short'
        return f'add_{side}' if abs(tgt) > abs(cur) else f'reduce_{side}'
    return 'flip_to_long' if tgt > 0 else 'flip_to_short'   # opposite-sign delta (defensive)


def _load_lambda(default: float = 2.0) -> float:
    """Read position_sizing_lambda from pipeline_config; fall back to default
    on any error. Bounded clamp guards against operator-pasted garbage.

    Cap = 2.0 (Reg T overnight max). 2026-05-19: tightened from 3.50 to
    2.00 to match Reg T overnight rule (50% initial margin → 2× equity
    gross). Pre-existing values above 2.0 silently clamp on next cycle —
    intentional, since trading over 2× overnight would violate Reg T."""
    try:
        import psycopg2
        with psycopg2.connect(os.environ['POSTGRES_URI']) as c:
            with c.cursor() as cur:
                cur.execute("SELECT value FROM pipeline_config WHERE key = 'position_sizing_lambda'")
                row = cur.fetchone()
                v = float(row[0]) if row else default
                return max(0.10, min(2.00, v))
    except Exception:
        return default


def _resolve_min_cumulative_sharpe(params: dict | None, default: float = 3.0) -> float:
    """Pick the conviction floor for the active regime.

    Used by _sharpe_cadence_path to drop tickers where the SIGNED sum of
    contributing strategies' effective_sharpe falls below this threshold.
    Naturally kills (a) low-conviction single-strategy bets and (b) tickers
    where opposing strategies nearly cancel out — the operator's
    'conflicting strategy information will cancel out' invariant.

    Per-regime as of 2026-05-21 (migration 108) — stored on
    regime_sizer_params.min_cumulative_sharpe, bound [1.0, 10.0] by the
    table's CHECK constraint. The sizer reads the value from the
    `params` dict already loaded for the active regime, so no extra DB
    round-trip is needed. Falls back to the legacy
    pipeline_config['min_cumulative_sharpe'] global if the per-regime
    field is missing (e.g. brand-new DB before the migration runs),
    then to `default` if that's also absent.
    """
    if isinstance(params, dict):
        v = params.get('min_cumulative_sharpe')
        if v is not None:
            try:
                return max(1.0, min(10.0, float(v)))
            except (TypeError, ValueError):
                pass
    try:
        import psycopg2
        with psycopg2.connect(os.environ['POSTGRES_URI']) as c:
            with c.cursor() as cur:
                cur.execute("SELECT value FROM pipeline_config WHERE key = 'min_cumulative_sharpe'")
                row = cur.fetchone()
                if row is not None:
                    return max(1.0, min(10.0, float(row[0])))
    except Exception:
        pass
    return default


def _load_approved_carried_signals(weight_by_strat: dict[str, float]) -> list[dict]:
    """SP-6 Phase A — load the APPROVED carried set for today's reconcile.

    In EOD mode the sizer loads execution_signals WHERE lifecycle_state='APPROVED'
    AND target_date=today instead of the legacy active-window query. These signals
    were already gated at 4PM[T] (EOD compute) + 9:15AM (pre-market carry-forward
    gate), so no additional age/cadence filter is applied here.

    Return shape is identical to _load_active_window_signals: a list of
    {strategy_id, ticker, direction, signal_date, entry_price, stop_loss,
     target_1, target_2} dicts ready for the ticker-weight aggregation loop.

    Filters to strategies present in weight_by_strat (same as the legacy
    loader); strategies absent from weights are excluded by the aggregation
    loop at L469 anyway, so this is a no-op guard, not a narrowing filter.
    Applies the same fail-open alpaca_tradable_universe filter as the legacy
    loader — dropped tickers are logged, not fatal.
    """
    import psycopg2
    import psycopg2.extras
    from datetime import date as _date

    if not weight_by_strat:
        return []

    today = _date.today()
    sids = list(weight_by_strat.keys())
    out = []
    tradable_symbols: set[str] = set()
    try:
        with psycopg2.connect(os.environ['POSTGRES_URI']) as c:
            with c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute('''
                    SELECT DISTINCT ON (strategy_id, ticker)
                           strategy_id, ticker, direction, signal_date,
                           entry_price, stop_loss, target_1, target_2,
                           signal_params
                    FROM execution_signals
                    WHERE lifecycle_state = 'APPROVED'
                      AND target_date = %s
                      AND strategy_id = ANY(%s)
                    ORDER BY strategy_id, ticker, signal_date DESC
                ''', (today, sids))
                rows = cur.fetchall()
                cur.execute("""SELECT symbol FROM alpaca_tradable_universe
                              WHERE status='active' AND tradable=TRUE""")
                tradable_symbols = {r[0] for r in cur.fetchall()}
    except Exception as e:
        logger.warning('sharpe_cadence EOD: approved-carried fetch failed (%s); falling back to empty', e)
        return []

    dropped_untradable = 0
    for r in rows:
        if tradable_symbols and r['ticker'] not in tradable_symbols:
            dropped_untradable += 1
            continue
        # SP-5.1c: parse signal_params (psycopg2 DictCursor may deserialize jsonb
        # to dict already; handle str defensively — mirrors trade_handoff_builder.py).
        sp = r['signal_params'] or {}
        if isinstance(sp, str):
            try:
                sp = json.loads(sp) if sp else {}
            except ValueError:
                sp = {}
        out.append({'strategy_id': r['strategy_id'], 'ticker': r['ticker'],
                    'direction': r['direction'], 'signal_date': r['signal_date'],
                    'entry_price': r['entry_price'], 'stop_loss': r['stop_loss'],
                    'target_1': r['target_1'], 'target_2': r['target_2'],
                    'signal_params': sp})
    if dropped_untradable:
        logger.info('sharpe_cadence EOD: dropped %d untradable signals (universe=%d)',
                    dropped_untradable, len(tradable_symbols))
    logger.info('sharpe_cadence EOD: loaded %d APPROVED carried signals for %s', len(out), today)
    return out


def _load_active_window_signals(regime_state: str, weight_by_strat: dict[str, float],
                                cadence_by_strat: dict[str, float]):
    """Pull every open signal still within its strategy's cadence window.

    "Information staying relevant in the period" per design spec: a
    monthly strategy's signal contributes for ~21 trading days; a weekly
    strategy's for 5; a daily strategy's for 1. We aggregate across the
    window, not just today's emissions.

    For each (strategy, ticker) pair we keep the **most recent** open
    signal — if a strategy re-fires earlier than expected, the latest
    direction supersedes the prior one.

    Returns list of {strategy_id, ticker, direction} dicts ready for
    the ticker_weight aggregation loop.
    """
    import psycopg2
    import psycopg2.extras
    from datetime import date as _date, timedelta as _timedelta

    if not weight_by_strat:
        return []

    today = _date.today()
    # Earliest signal_date that could still be active: today - max cadence.
    # No weekend buffer: cadence_days is now sourced from live avg_holding_days
    # (in strategy_weights._load_active_strategies), so a strategy that
    # appears stale on Monday morning genuinely hasn't generated new info.
    # The user's invariant (2026-05-19): only fresh information from the
    # cycle's actual cadence window — no expired information.
    # Coarse SQL lower-bound: math.ceil so the query never tightens past the
    # precise per-strategy filter below. cadence_by_strat values are floats
    # (NUMERIC(10,4) post-mig-122).
    import math as _math
    max_cad = max(cadence_by_strat.get(s, 1.0) for s in weight_by_strat) if cadence_by_strat else 1.0
    earliest = today - _timedelta(days=_math.ceil(max_cad))

    sids = list(weight_by_strat.keys())
    out = []
    tradable_symbols: set[str] = set()
    try:
        with psycopg2.connect(os.environ['POSTGRES_URI']) as c:
            with c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute('''
                    SELECT DISTINCT ON (strategy_id, ticker)
                           strategy_id, ticker, direction, signal_date,
                           entry_price, stop_loss, target_1, target_2,
                           signal_params
                    FROM execution_signals
                    WHERE status = 'open'
                      AND strategy_id = ANY(%s)
                      AND signal_date >= %s
                    ORDER BY strategy_id, ticker, signal_date DESC
                ''', (sids, earliest))
                rows = cur.fetchall()
                # Load the Alpaca tradable universe so delisted / halted /
                # unsupported tickers (FX, indexes, crypto) get dropped at
                # the active-window boundary — trade_handoff_builder's
                # filter doesn't apply here because the sharpe_cadence path
                # reads execution_signals directly. Fail-open on empty
                # set so a missing universe table can't halt trading.
                cur.execute("""SELECT symbol FROM alpaca_tradable_universe
                              WHERE status='active' AND tradable=TRUE""")
                tradable_symbols = {r[0] for r in cur.fetchall()}
    except Exception as e:
        logger.warning('sharpe_cadence: active-window fetch failed (%s); falling back to today-only', e)
        return []
    # Per-strategy cadence filter: drop signals older than the strategy's
    # own window. The SQL query above used max_cad as a coarse upper
    # bound; here we tighten per strategy. Also drop tickers not in the
    # tradable universe (fail-open when set is empty).
    dropped_untradable = 0
    dropped_stale = 0
    for r in rows:
        sid = r['strategy_id']
        cad = cadence_by_strat.get(sid, 1.0)
        age = (today - r['signal_date']).days
        # Strict cadence: a cad=1 (daily) strategy contributes ONLY today's
        # signal; a cad=4 (4-day-holding) strategy contributes the last
        # 4 days of signals (age in [0,3]). Drop everything older.
        if age >= cad:
            dropped_stale += 1
            continue
        if tradable_symbols and r['ticker'] not in tradable_symbols:
            dropped_untradable += 1
            continue
        # SP-5.1c: parse signal_params (psycopg2 DictCursor may deserialize jsonb
        # to dict already; handle str defensively — mirrors trade_handoff_builder.py).
        sp = r['signal_params'] or {}
        if isinstance(sp, str):
            try:
                sp = json.loads(sp) if sp else {}
            except ValueError:
                sp = {}
        out.append({'strategy_id': sid, 'ticker': r['ticker'],
                    'direction': r['direction'], 'signal_date': r['signal_date'],
                    'entry_price': r['entry_price'], 'stop_loss': r['stop_loss'],
                    'target_1': r['target_1'], 'target_2': r['target_2'],
                    'signal_params': sp})
    if dropped_untradable:
        logger.info('sharpe_cadence: dropped %d untradable signals (universe=%d)',
                    dropped_untradable, len(tradable_symbols))
    if dropped_stale:
        logger.info('sharpe_cadence: dropped %d stale signals (age >= cadence_days)', dropped_stale)
    return out


# SP-5.1a (G1 prong 2): OCC option symbol pattern, e.g. SPY260626C00755000
# (1-6 char root incl. '.', YYMMDD expiry, C/P, 8-digit strike×1000). Held option
# legs surface in the broker book as OCC symbols; the equity-book loader must
# exclude them so neither this sizer's _classify_position_deltas nor open_reconcile's
# run_reconcile (same loader + classifier) orphan-closes a held leg as equity.
_OCC_RE = re.compile(r'^[A-Z.]{1,6}\d{6}[CP]\d{8}$')


def _is_occ_symbol(sym) -> bool:
    """True iff `sym` is an OCC-formatted option leg symbol (not an equity/crypto)."""
    if not sym:
        return False
    return bool(_OCC_RE.match(str(sym).strip().upper()))


def _expiry_close_dte() -> int:
    """SP-5.3 threshold resolver: env override or the module default."""
    raw = os.environ.get('OPENCLAW_OPTION_EXPIRY_CLOSE_DTE')
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning('expiry_close_dte: unparseable override %r — using default %d',
                           raw, OPTION_EXPIRY_CLOSE_DTE)
    return OPTION_EXPIRY_CLOSE_DTE


def _occ_dte(occ: str, today=None):
    """SP-5.3 (C1): calendar days to expiry from the OCC tail (yymmdd at occ[-15:-9];
    the _OCC_RE filter upstream guarantees 6 digits there). Parse failure → DTE 0
    + loud warning: an unreadable expiry is an unmanageable position — close it."""
    import datetime as _dt
    today = today or _dt.date.today()
    try:
        tail = occ[-15:-9]
        exp = _dt.date(2000 + int(tail[:2]), int(tail[2:4]), int(tail[4:6]))
        return (exp - today).days
    except (ValueError, IndexError):
        logger.warning('occ_dte: unparseable expiry on %r — treating as DTE 0 (expiring)', occ)
        return 0


def _load_broker_positions_usd():
    """Read current broker positions as {ticker: signed_market_value_usd}.

    Positive = long, negative = short. Returns empty dict on any failure
    (fail-safe: the sizer then behaves as if book is flat — emits target
    orders, executor's idempotency catches duplicates).

    SP-5.1a: OCC option legs (e.g. SPY260626C00755000) are EXCLUDED — this loader
    feeds the equity-book delta/orphan logic in both the daily sizer and the 9:28
    run_reconcile; an option leg keyed by its raw OCC symbol would be orphan_close'd
    as if it were equity. The option position lifecycle is owned by the executor +
    EOD carried set, not the equity book.
    """
    import subprocess
    import json as _json
    try:
        env = {**os.environ}
        # Alpaca CLI auto-detects ALPACA_API_KEY/SECRET via env.
        proc = subprocess.run(['/root/go/bin/alpaca', 'position', 'list'],
                              capture_output=True, env=env, timeout=15)
        if proc.returncode != 0:
            logger.warning('sharpe_cadence: broker position fetch failed (%s)', proc.stderr.decode()[:200])
            return {}
        positions = _json.loads(proc.stdout)
        out = {}
        excluded_legs = []
        for p in positions:
            try:
                qty = float(p.get('qty', 0))
                mkt = float(p.get('market_value', 0))
                if qty == 0:
                    continue
                sym = p['symbol']
                if _is_occ_symbol(sym):
                    excluded_legs.append(sym)
                    continue
                # market_value is signed (negative for shorts); use that.
                out[sym] = mkt
            except (TypeError, ValueError):
                continue
        if excluded_legs:
            logger.info('sharpe_cadence: excluded %d held option leg(s) from equity book: %s',
                        len(excluded_legs), ', '.join(sorted(excluded_legs)))
        return out
    except Exception as e:
        logger.warning('sharpe_cadence: broker fetch error (%s)', e)
        return {}


def _held_option_underlyings() -> dict[str, list[str]]:
    """SP-5 Phase 1b (G4b): held OCC option legs grouped by underlying root.

    Returns {underlying: [occ, ...]} for every held OCC option leg on the broker
    book. Underlying root = occ[:-15] (an OCC symbol is root + 6-digit yymmdd +
    C/P + 8-digit strike = a fixed 15-char tail). Non-OCC positions (equity, crypto)
    and zero-qty rows are ignored.

    Runs the SAME `alpaca position list` CLI pattern as _load_broker_positions_usd
    (subprocess, 15s timeout, fail-safe {} on ANY error). Used to orphan-close a held
    option structure whose option signal has disappeared — the post-G1 equity book
    logic is OCC-filtered and structurally blind to option legs, so nothing else
    closes them.
    """
    import subprocess
    import json as _json
    try:
        env = {**os.environ}
        proc = subprocess.run(['/root/go/bin/alpaca', 'position', 'list'],
                              capture_output=True, env=env, timeout=15)
        if proc.returncode != 0:
            logger.warning('held_option_underlyings: broker fetch failed (%s)',
                           proc.stderr.decode()[:200])
            return {}
        positions = _json.loads(proc.stdout)
        from collections import defaultdict
        out = defaultdict(list)
        for p in positions:
            try:
                if float(p.get('qty', 0)) == 0:
                    continue
                sym = str(p['symbol']).strip().upper()
                if not _is_occ_symbol(sym):
                    continue
                out[sym[:-15]].append(sym)
            except (TypeError, ValueError, KeyError):
                continue
        return dict(out)
    except Exception as e:
        logger.warning('held_option_underlyings: broker fetch error (%s)', e)
        return {}


def _classify_position_deltas(
    target_usd: dict[str, float],
    broker: dict[str, float],
    ticker_meta: dict,
) -> list[tuple[str, float, str]]:
    """Classify position deltas into emission kinds: delta, flip_close, flip_open, orphan_close.

    Pure function: no DB, no broker calls. Takes target positions, current broker positions,
    and ticker metadata; returns a list of (ticker, signed_usd, kind) tuples.

    Each ticker is classified as one of:
      • delta         — single order; close_only auto-detected downstream when the delta
                        direction has no aligned bracket.
      • orphan_close  — ticker held but absent from current targets;
                        strategy_id='__close_orphan__', tier-0 in executor.
      • flip_close    — current and target have OPPOSITE signs. Liquidates the existing
                        position fully. Tier-1 in executor. Paired with flip_open below;
                        flip_open must wait for the close to fill (executor polls). Alpaca
                        bracket orders do not support auto-reverse so we CANNOT submit a
                        single oversize bracket.
      • flip_open     — the paired new-direction open following flip_close. Tier-2 (short)
                        or tier-3 (long) in executor.

    Args:
        target_usd:  dict[str, float] — target position in USD, per ticker
        broker:      dict[str, float] — current broker positions in USD
                     (positive=long, negative=short)
        ticker_meta: dict — mutable; will be populated with __close_orphan__ metadata
                     for orphan tickers not already present

    Returns:
        list[tuple[str, float, str]] — each (ticker, signed_usd, kind) emission
    """
    # First pass: identify flips (opposite-sign transitions).
    flip_tickers: set[str] = set()
    for tkr, target in target_usd.items():
        current = broker.get(tkr, 0.0)
        if current == 0.0 or target == 0.0:
            continue
        # Opposite-sign check
        if (target > 0 > current) or (target < 0 < current):
            flip_tickers.add(tkr)

    # Second pass: emit deltas and flips for all targets.
    emissions: list[tuple[str, float, str]] = []
    for tkr, target in target_usd.items():
        current = broker.get(tkr, 0.0)
        if tkr in flip_tickers:
            emissions.append((tkr, -current, 'flip_close'))
            emissions.append((tkr,  target,  'flip_open'))
        else:
            delta = target - current
            if delta != 0.0:
                emissions.append((tkr, delta, 'delta'))

    # Third pass: emit orphan_closes for positions in broker but not in targets.
    for tkr, current in broker.items():
        if tkr in target_usd or current == 0.0:
            continue
        emissions.append((tkr, -current, 'orphan_close'))
        if tkr not in ticker_meta:
            ticker_meta[tkr] = {'strategies': ['__close_orphan__'], 'directions': [0],
                                'brackets': []}

    return emissions


def _inject_option_hedge_targets(cur, target_usd, account_state):
    """SP-5.1b-ii: add option delta-hedge share targets ON TOP of the normalized
    equity target_usd (post-normalization, cap-exempt — a hedge is a requirement,
    not a discretionary allocation). Reads APPROVED is_hedge execution_signals rows
    (written by the EOD compute_option_hedge_targets step) carrying hedge_shares in
    signal_params; injects signed shares*price into target_usd[underlying].

    Headroom guard (operator decision): the hedge adds gross on TOP of equity. If
    the hedge's gross would breach available buying power (after equity), scale the
    hedge DOWN proportionally + WARN; if no headroom, inject nothing + WARN. NEVER
    reduce equity; never silently zero a hedge without logging.
    """
    import json
    cur.execute(
        """SELECT ticker, direction, signal_params FROM execution_signals
           WHERE lifecycle_state='APPROVED' AND COALESCE(signal_params->>'is_hedge','')='true'
                 AND target_date = (SELECT MAX(target_date) FROM execution_signals
                                    WHERE lifecycle_state='APPROVED' AND COALESCE(signal_params->>'is_hedge','')='true')""")
    rows = cur.fetchall()
    if not rows:
        return target_usd
    # signed hedge $ per ticker
    hedge_usd = {}
    for ticker, direction, sparams in rows:
        sp = sparams if isinstance(sparams, dict) else json.loads(sparams or '{}')
        shares = float(sp.get('hedge_shares') or 0.0)
        if shares <= 0:
            continue
        px = _spot_price(ticker)
        if not px:
            continue
        signed = shares * px * (1.0 if str(direction).upper() == 'LONG' else -1.0)
        hedge_usd[ticker] = hedge_usd.get(ticker, 0.0) + signed
    if not hedge_usd:
        return target_usd
    # Headroom guard: hedge gross on top of equity gross must fit buying power.
    nav = float(account_state.get('equity') or 100_000.0)
    bp = float(account_state.get('buying_power') or account_state.get('daytrading_buying_power') or (4.0 * nav))
    equity_gross = sum(abs(v) for v in target_usd.values())
    hedge_gross = sum(abs(v) for v in hedge_usd.values())
    headroom = bp - equity_gross
    if hedge_gross > 0 and headroom <= 0:
        logger.warning('option-hedge: no buying-power headroom (bp=%.0f equity_gross=%.0f); hedge NOT injected', bp, equity_gross)
        return target_usd
    if hedge_gross > headroom > 0:
        scale = headroom / hedge_gross
        logger.warning('option-hedge: hedge gross $%.0f > headroom $%.0f; scaling hedge by %.3f', hedge_gross, headroom, scale)
        hedge_usd = {t: v * scale for t, v in hedge_usd.items()}
    for t, v in hedge_usd.items():
        target_usd[t] = target_usd.get(t, 0.0) + v
    return target_usd


def _warn_options_dropped_no_equity_scale(opt_active, reason: str) -> None:
    """SP-5.1a (G1 prong 1) fail-closed: option orders are sized at the equity path's
    per-unit-weight USD `scale`. If the equity side produced no scale (e.g. no equity
    signals at all), we cannot size options safely — emit NO option orders and log
    loudly. A future refinement could self-scale options against NAV directly."""
    if opt_active:
        sids = sorted({s.get('strategy_id') for s in opt_active if s.get('strategy_id')})
        logger.warning(
            'regime_blended_sizer.sharpe_cadence: %d option contributor(s) DROPPED '
            '(no equity scale: %s) — option strategies %s emitted ZERO orders this cycle '
            '(fail-closed; options size at the equity path scale)',
            len(opt_active), reason, sids)


def _consolidate_option_orders(opt_active, weight_by_strat, sharpe_by_strat, scale,
                               nav, min_cum_sharpe, account_state, equity_gross):
    """SP-5.1a (G1 prong 1 + G2): build per-UNDERLYING option orders from the
    partitioned option-class contributors. Mirrors the equity emission loop's order
    shape, with option-specific semantics:

      • per-UNDERLYING aggregation over option contributors only:
          w = Σ weight×dir (same _dir_to_int), net_sharpe = Σ sharpe×dir
      • SAME min_cum_sharpe drop (|net_sharpe| < floor → drop the group)
      • option_spec = first contributor's spec; a LATER contributor with a DIFFERENT
        spec logs a loud warning (first-wins) — keeps the structure deterministic
      • strategy_id = option-only composite '|'.join(sorted(set(sids)))[:120]  (G2:
        stable, option-only contributor set ⇒ idempotent option_hedge_ledger key)
      • notional_usd = abs(w × scale)  (per-unit-weight USD parity with the equity path)
      • emission semantics = plain open: options NEVER net against the equity broker
        map, never flip, never orphan (current_usd=0.0; action via _derive_action with
        kind='delta', current=0.0). The option position lifecycle is owned by the
        executor + EOD carried set.
      • brackets all None; instrument_class='option'; source_mode='sharpe_cadence'.

    ON-TOP HEADROOM GUARD (mirrors _inject_option_hedge_targets): option gross adds on
    TOP of the equity book. headroom = buying_power − equity_gross. If option gross >
    headroom > 0 → scale ALL option notionals down proportionally + WARN; if
    headroom <= 0 → emit NO option orders + WARN. NEVER touch equity targets.

    Returns a list of order dicts (same keys as the equity emission loop builds).
    """
    if not opt_active:
        return []

    from collections import defaultdict
    u_w = defaultdict(float)
    u_net_sharpe = defaultdict(float)
    u_sids = defaultdict(list)        # contributor sids per underlying (in arrival order)
    u_spec = {}                       # first-wins spec per underlying

    for s in opt_active:
        sid = s.get('strategy_id')
        tkr = s.get('ticker')
        if not sid or not tkr or sid not in weight_by_strat:
            continue
        d = _dir_to_int(s.get('direction'))
        if d == 0:
            continue
        spec = (s.get('signal_params') or {}).get('option_spec')
        if not spec:
            continue
        u_w[tkr] += weight_by_strat[sid] * d
        u_net_sharpe[tkr] += sharpe_by_strat.get(sid, 0.0) * d
        u_sids[tkr].append(sid)
        if tkr not in u_spec:
            u_spec[tkr] = spec
        elif u_spec[tkr] != spec:
            logger.warning(
                'regime_blended_sizer.sharpe_cadence: CONFLICTING option_spec on %s '
                '(strategy %s spec differs from first-seen) — keeping FIRST spec %s, '
                'ignoring the conflicting one', tkr, sid, u_spec[tkr])

    if not u_w:
        return []

    # SAME conviction gate as the equity path.
    if min_cum_sharpe > 0:
        dropped = [tkr for tkr in list(u_w.keys())
                   if abs(u_net_sharpe.get(tkr, 0.0)) < min_cum_sharpe]
        for tkr in dropped:
            u_w.pop(tkr, None)
        if dropped:
            logger.info('regime_blended_sizer.sharpe_cadence: dropped %d option group(s) '
                        'below min_cum_sharpe=%.2f', len(dropped), min_cum_sharpe)
    if not u_w:
        return []

    # Per-unit-weight USD parity with the equity scale.
    opt_usd = {tkr: w * scale for tkr, w in u_w.items()}

    # ON-TOP headroom guard (mirrors _inject_option_hedge_targets). equity_gross is the
    # actual Σ|target_usd| of the equity targets (passed by the caller after the
    # min-notional renorm); option gross adds on TOP of it.
    bp = float(account_state.get('buying_power')
               or account_state.get('daytrading_buying_power')
               or (4.0 * nav))
    opt_gross = sum(abs(v) for v in opt_usd.values())
    headroom = bp - equity_gross
    if opt_gross > 0 and headroom <= 0:
        logger.warning('option-consolidate: no buying-power headroom (bp=%.0f equity_gross=%.0f); '
                       '%d option order(s) NOT emitted', bp, equity_gross, len(opt_usd))
        return []
    if opt_gross > headroom > 0:
        dn = headroom / opt_gross
        logger.warning('option-consolidate: option gross $%.0f > headroom $%.0f; scaling options by %.4f',
                       opt_gross, headroom, dn)
        opt_usd = {t: v * dn for t, v in opt_usd.items()}

    orders = []
    for tkr, usd in opt_usd.items():
        dir_sign = 1 if usd >= 0 else -1
        sid_out = '|'.join(sorted(set(u_sids[tkr])))[:120]
        orders.append({
            'ticker':                  tkr,
            'strategy_id':             sid_out,
            'direction':               'long' if dir_sign > 0 else 'short',
            'notional_usd':            abs(usd),
            'pct_nav':                 abs(usd) / nav,
            'shares':                  0,
            'entry':                   None,
            'stop':                    None,
            't1':                      None,
            't2':                      None,
            'kelly_final':             abs(usd) / nav,
            'ev':                      0.0,
            'p_t1':                    0.5,
            'source_mode':             'sharpe_cadence',
            'target_usd':              usd,
            'current_usd':             0.0,   # options never net the equity broker map
            'contributing_strategies': list(u_sids[tkr]),
            'flip_action':             None,
            'action':                  _derive_action('delta', 0.0, usd, dir_sign),
            'instrument_class':        'option',
            'option_spec':             u_spec[tkr],
        })
    return orders


def _sharpe_cadence_path(signals, account_state, regime_state, params, confirmer):
    """Sharpe × cadence × direction sizer with cadence-window aggregation
    AND broker-position netting.

    Steps:
      1. Pull per-(strategy, regime) daily_weight from
         strategy_weights_by_regime (load_current).
      2. Pull every still-relevant open signal from the DB (any open
         signal whose age is within its strategy's cadence_days). Latest
         signal per (strategy, ticker) wins.
      3. For each ticker, sum daily_weight × direction → ticker_weight.
      4. Normalize so Σ |target_usd| = λ × NAV exactly. No per-ticker
         caps and no minimum-notional floor — high-conviction tickers
         (multi-strategy agreement) receive proportional allocation.
      5. **Rebalance step**: read broker positions; the order_delta for
         each ticker = target_usd − current_position_usd. Tickers held
         but no longer signalled → emit close orders. This keeps daily
         PV consumption = λ × NAV instead of stacking.
      6. TradeJohn keep|cancel on the surviving deltas.
      7. Emit orders in the sized-handoff payload shape.

    The `signals` parameter (today's emissions) is now ignored in favour
    of the DB query — anything emitted today is already persisted to
    execution_signals by the signals step earlier in the pipeline.
    """
    from execution import strategy_weights as _sw

    nav = float(account_state.get('equity') or 100_000.0)
    rows = _sw.load_current(regime_state)
    if not rows:
        logger.info('regime_blended_sizer.sharpe_cadence: no current weights for %s', regime_state)
        return []
    weight_by_strat   = {r['strategy_id']: float(r['daily_weight']) for r in rows}
    sharpe_by_strat   = {r['strategy_id']: float(r['effective_sharpe']) for r in rows}
    cadence_by_strat  = {r['strategy_id']: float(r['cadence_days']) for r in rows}
    # Effective leverage = global λ × per-regime liquidity_param.
    # liquidity_param is a per-regime DAMPENER (∈ [0, 1.0]); paired with
    # lam_global ∈ [0.10, 2.00] this guarantees effective lam ≤ 2.0 (Reg T
    # overnight rule, 50% initial margin). The 1.0 cap matches the DB CHECK
    # constraint on regime_sizer_params.liquidity_param so a misconfigured
    # row can't slip past the sizer. Historically sharpe_cadence skipped
    # liquidity_param entirely; operator added it 2026-05-16 so per-regime
    # target gross matches the documented intent (TRANSITIONING 1.5×, etc.).
    lam_global   = _load_lambda()
    liq_regime   = float(params.get('liquidity_param', 1.0)) if params else 1.0
    lam = lam_global * max(0.0, min(1.0, liq_regime))
    min_cum_sharpe = _resolve_min_cumulative_sharpe(params)

    # Signal loading: EOD mode loads APPROVED carried set (SP-6 Phase A);
    # legacy path loads the cadence-window active signals. Gate OFF is the
    # legacy _load_active_window_signals call, unchanged.
    if os.environ.get('OPENCLAW_EOD_RECONCILE') == '1':
        active = _load_approved_carried_signals(weight_by_strat)
    else:
        # Fix A: aggregate across cadence-window, not today-only.
        active = _load_active_window_signals(regime_state, weight_by_strat, cadence_by_strat)
    if not active:
        if os.environ.get('OPENCLAW_EOD_RECONCILE') == '1':
            # EOD mode: an empty APPROVED set means every carried signal was
            # vetoed (or none were computed). Do NOT fall back to the handoff
            # signals — those may contain COMPUTED/legacy rows the pre-market
            # gate deliberately rejected. Zero orders here is correct; the
            # run_reconcile step (Task 8b) handles the flatten path when
            # appropriate.
            logger.info('regime_blended_sizer.sharpe_cadence: EOD mode — no APPROVED carried signals; '
                        'zero orders (flatten handled by run_reconcile if needed)')
            return []
        # Legacy fallback: use today's `signals` parameter (e.g. force_all day-1
        # of regime, before signals are persisted)
        active = signals or []
        logger.info('regime_blended_sizer.sharpe_cadence: active-window empty, using today\'s signals (%d)', len(active))
    else:
        if os.environ.get('OPENCLAW_EOD_RECONCILE') == '1':
            logger.info('regime_blended_sizer.sharpe_cadence: EOD mode — %d APPROVED carried signals', len(active))
        else:
            logger.info('regime_blended_sizer.sharpe_cadence: %d active-window signals across all cadences', len(active))

    # SP-5.1a (G1 prong 1): partition by instrument_class at the source. Option
    # contributors (signal_params carries an option_spec) are pulled OUT of `active`
    # so the entire equity pipeline below runs UNCHANGED on the equity partition.
    # When no option signals exist (production today) opt_active is empty and this is
    # a provable no-op — `active` is identical and every equity order is byte-identical
    # (the dead SP-5.1c attribution + emission blocks have been removed accordingly).
    # Runs in BOTH legacy and EOD modes (it follows whichever loader populated `active`).
    # Deliberately BEFORE the orthogonalization fold: option signals must never
    # participate in equity strategy-similarity folding (a fold_map grouping an
    # option sid with an equity representative would otherwise silently DROP the
    # option contributor before it could reach the partition).
    opt_active = [s for s in active
                  if ((s.get('signal_params') or {}).get('option_spec'))]
    if opt_active:
        _opt_ids = {id(s) for s in opt_active}
        active = [s for s in active if id(s) not in _opt_ids]
        logger.info('regime_blended_sizer.sharpe_cadence: partitioned %d option-class '
                    'contributor(s) out of the equity book', len(opt_active))

    # Strategy orthogonalization (default-OFF; byte-identical when both gates unset).
    _ortho_groups = None
    if _ortho_enabled('OPENCLAW_STRATEGY_FOLD') or _ortho_enabled('OPENCLAW_STRATEGY_CORR_WEIGHT') \
            or _ortho_enabled('OPENCLAW_STRATEGY_ORTHO_SHADOW') \
            or _ortho_enabled('OPENCLAW_STRATEGY_BRACKET_STACK'):
        try:
            from execution import strategy_similarity as _ss
            _ortho_groups = _ss.load_groups(regime_state)
        except Exception as e:
            logger.warning('orthogonalization: load_groups failed (%s); proceeding without', e)
            _ortho_groups = None

    if _ortho_groups and _ortho_enabled('OPENCLAW_STRATEGY_FOLD'):
        from execution import orthogonalization as _og
        before = len(active)
        active = _og.fold_active_contributions(
            active, _ortho_groups['fold_map'], _ortho_groups['rep_map'], sharpe_by_strat)
        logger.info('orthogonalization.fold: %d -> %d contributions', before, len(active))

    # Aggregate ticker_weight across signalling strategies. ticker_net_sharpe
    # is the SIGNED sum of effective_sharpe — opposing strategies cancel
    # (so a long_sharpe=5 + short_sharpe=4 ticker collapses to |1|, dropped
    # by the gate below). Unsigned sum would let conflicting tickers slip
    # through.
    from collections import defaultdict
    ticker_w = defaultdict(float)
    ticker_net_sharpe = defaultdict(float)
    ticker_meta = defaultdict(lambda: {'strategies': [], 'directions': [], 'brackets': []})
    for s in active:
        sid = s.get('strategy_id')
        tkr = s.get('ticker')
        if not sid or not tkr or sid not in weight_by_strat:
            continue
        d = _dir_to_int(s.get('direction'))
        if d == 0:
            continue
        ticker_w[tkr] += weight_by_strat[sid] * d
        ticker_net_sharpe[tkr] += sharpe_by_strat.get(sid, 0.0) * d
        ticker_meta[tkr]['strategies'].append(sid)
        ticker_meta[tkr]['directions'].append(d)
        # Direction-leader bracket pick (Phase 1 spec #6): the largest-weight
        # contribution in the *winning* direction wins. We keep every
        # (direction, weight, bracket) tuple here; final selection happens
        # after ticker_w sign is known.
        ticker_meta[tkr]['brackets'].append({
            'sid':        sid,
            'direction':  d,
            'weight':     weight_by_strat[sid],
            'entry':      s.get('entry_price'),
            'stop':       s.get('stop_loss'),
            't1':         s.get('target_1'),
            't2':         s.get('target_2'),
        })
        # SP-5.1a (G1 prong 1): option contributors are partitioned out of `active`
        # above, so they never reach this equity loop. The former SP-5.1c per-ticker
        # option_spec attribution block lived here; it is now dead and removed —
        # equity orders therefore NEVER carry option_spec/instrument_class by
        # construction (option orders are built by _consolidate_option_orders below).

    if not ticker_w:
        logger.info('regime_blended_sizer.sharpe_cadence: no eligible signals after weight filter')
        _warn_options_dropped_no_equity_scale(opt_active, 'no eligible equity signals after weight filter')
        return []

    # Tier-2: deflate the conviction gate by effective-independent-bets (gate only; sizing untouched).
    gate_net_sharpe = ticker_net_sharpe
    if _ortho_groups and _ortho_enabled('OPENCLAW_STRATEGY_CORR_WEIGHT'):
        from execution import orthogonalization as _og
        contribs_by_ticker = {
            tkr: list(zip(meta['strategies'], meta['directions']))
            for tkr, meta in ticker_meta.items()
        }
        gate_net_sharpe = _og.deflated_net_sharpe(
            contribs_by_ticker, _ortho_groups['block_map'],
            _ortho_groups['matrix'], sharpe_by_strat)
        logger.info('orthogonalization.corr_weight: deflated gate for %d tickers', len(gate_net_sharpe))

    # Cumulative-sharpe gate: drop tickers whose signed net_sharpe falls
    # below the configured floor (default 3.0, from pipeline_config). This
    # is the operator's primary conviction filter — kills single-strategy
    # bets AND near-cancellation tickers in one rule.
    if min_cum_sharpe > 0:
        gated_out = [tkr for tkr in list(ticker_w.keys())
                     if abs(gate_net_sharpe.get(tkr, 0.0)) < min_cum_sharpe]
        for tkr in gated_out:
            ticker_w.pop(tkr, None)
            ticker_meta.pop(tkr, None)
        if gated_out:
            logger.info('regime_blended_sizer.sharpe_cadence: dropped %d tickers below min_cum_sharpe=%.2f (kept=%d)',
                        len(gated_out), min_cum_sharpe, len(ticker_w))

    if not ticker_w:
        logger.info('regime_blended_sizer.sharpe_cadence: no tickers cleared cum_sharpe gate')
        _warn_options_dropped_no_equity_scale(opt_active, 'no equity tickers cleared cum_sharpe gate')
        return []

    # Shadow: log what orthogonalization WOULD change without affecting routing.
    if _ortho_groups and _ortho_enabled('OPENCLAW_STRATEGY_ORTHO_SHADOW') \
            and not (_ortho_enabled('OPENCLAW_STRATEGY_FOLD') or _ortho_enabled('OPENCLAW_STRATEGY_CORR_WEIGHT')):
        try:
            from execution import orthogonalization as _og
            shadow_contribs = {
                tkr: list(zip(meta['strategies'], meta['directions']))
                for tkr, meta in ticker_meta.items()
            }
            shadow_gate = _og.deflated_net_sharpe(
                shadow_contribs, _ortho_groups['block_map'],
                _ortho_groups['matrix'], sharpe_by_strat)
            would_drop = [t for t in ticker_w
                          if abs(shadow_gate.get(t, 0.0)) < min_cum_sharpe]
            mat = _ortho_groups.get('matrix') or {}
            offdiag = [mat[a][b] for a in mat for b in mat.get(a, {}) if a != b]
            hist = {}
            for v in offdiag:
                bucket = round(float(v) * 10) / 10
                hist[bucket] = hist.get(bucket, 0) + 1
            logger.info('orthogonalization.shadow: would_drop=%s similarity_histogram=%s '
                        'fold_pairs=%d block_pairs=%d',
                        would_drop, dict(sorted(hist.items())),
                        len(_ortho_groups['fold_map']), len(_ortho_groups['block_map']))
        except Exception as e:
            logger.warning('orthogonalization.shadow failed (%s)', e)

    gross = sum(abs(w) for w in ticker_w.values())
    if gross <= 0:
        _warn_options_dropped_no_equity_scale(opt_active, 'equity gross is zero')
        return []
    scale = (lam * nav) / gross

    # Pure formulation (2026-05-14): target_usd = ticker_w × (λ × NAV / Σ|ticker_w|).
    # High-conviction tickers (sum of contributing strategy weights × direction)
    # get proportionally more allocation; no per-ticker cap, no redistribute
    # pool. The operator explicitly accepts the concentration trade-off in
    # exchange for honest expression of conviction.
    target_usd: dict[str, float] = {tkr: w * scale for tkr, w in ticker_w.items()}

    # Per-regime min-notional gate (2026-05-16). Drop tickers whose target
    # falls below NAV × min_signal_notional_pct, then renormalize the
    # survivors so total gross still equals λ × NAV. This pushes the
    # freed capital into higher-conviction names — matches operator's
    # "fewer, larger positions" goal. Falls back to the legacy USD column
    # if the new pct column isn't present yet (pre-migration safety).
    min_notional_pct = float(params.get('min_signal_notional_pct') or 0.0) if params else 0.0
    if min_notional_pct <= 0 and params and params.get('min_signal_notional_usd'):
        min_notional_pct = float(params['min_signal_notional_usd']) / max(nav, 1.0)
    min_notional_dollars = nav * min_notional_pct
    if min_notional_dollars > 0:
        dropped_small = [t for t, v in target_usd.items() if abs(v) < min_notional_dollars]
        for tkr in dropped_small:
            target_usd.pop(tkr, None)
            ticker_w.pop(tkr, None)
            ticker_meta.pop(tkr, None)
        if dropped_small:
            new_gross = sum(abs(w) for w in ticker_w.values())
            if new_gross > 0:
                new_scale = (lam * nav) / new_gross
                target_usd = {t: w * new_scale for t, w in ticker_w.items()}
                # SP-5 Phase 1a (review NIT): options size at the EFFECTIVE
                # equity per-unit-weight rate — after a min-notional renorm the
                # post-renorm scale is the rate equity actually trades at.
                scale = new_scale
            logger.info('regime_blended_sizer.sharpe_cadence: min-notional gate dropped %d (<$%.0f = %.3f%% NAV); renormalized %d survivors',
                        len(dropped_small), min_notional_dollars, min_notional_pct * 100, len(target_usd))

    # SP-6 per-ticker conviction cap (operator formula, 2026-06-04):
    #     cap_usd(t) = PER_TICKER_CAP_SHARPE_FRAC × |gate_net_sharpe(t)| × λ × NAV
    # Bounds the few-survivor concentration failure: Σ|target| = λ×NAV
    # renormalizes onto whatever cleared the cum-sharpe gate, so a 1-survivor
    # day would otherwise put the ENTIRE λ×NAV into one name (2026-06-03
    # diagnosis: $224.8k = 2× equity into STX; see
    # /root/sp6_phaseA_conviction_gate_diagnosis_2026-06-04.md §4). The cap
    # scales with the SAME conviction measure the gate uses (deflated when
    # OPENCLAW_STRATEGY_CORR_WEIGHT is on, raw otherwise) so high-conviction
    # names keep proportionally more headroom. Shaved capital is deliberately
    # NOT redistributed (no renorm-up — that would re-concentrate elsewhere);
    # on capped days gross simply lands below λ×NAV. Applied AFTER the
    # min-notional renorm (which pushes freed capital into high-conviction
    # names — exactly the flow the cap must bound) and BEFORE the cap-exempt
    # option hedge injection + broker netting. EOD-mode only: rides
    # OPENCLAW_EOD_RECONCILE so the legacy cadence-window path stays
    # byte-identical (its multi-day accumulation makes few-survivor days
    # structurally rare). Missing gate value → fail-open (target untouched).
    if os.environ.get('OPENCLAW_EOD_RECONCILE') == '1':
        _capped = []
        for _tkr, _usd in list(target_usd.items()):
            _gs = gate_net_sharpe.get(_tkr)
            if _gs is None:
                continue
            _cap = PER_TICKER_CAP_SHARPE_FRAC * abs(_gs) * lam * nav
            if abs(_usd) > _cap:
                target_usd[_tkr] = _cap if _usd > 0 else -_cap
                _capped.append((_tkr, _usd, target_usd[_tkr]))
        if _capped:
            logger.info(
                'regime_blended_sizer.sharpe_cadence: per-ticker cap (0.05·|sharpe|·λ·NAV) '
                'clamped %d ticker(s): %s',
                len(_capped),
                [(t, round(o), round(n)) for t, o, n in _capped[:10]])

    # SP-5.1b-ii: inject option delta-hedge targets ON TOP of the normalized
    # equity target_usd (post-normalization, cap-exempt). Gate default-OFF:
    # equity target_usd is byte-identical when gate is unset.
    if os.environ.get('OPENCLAW_OPTION_DELTA_HEDGE') == '1':
        import psycopg2
        with psycopg2.connect(os.environ['POSTGRES_URI']) as _hc:
            with _hc.cursor() as _hcur:
                target_usd = _inject_option_hedge_targets(_hcur, target_usd, account_state)

    # Rebalance against current broker positions. Each ticker is classified as
    # one of four emission kinds:
    #   • delta         — single order; close_only auto-detected downstream
    #                     when the delta direction has no aligned bracket.
    #   • orphan_close  — ticker held but absent from current targets;
    #                     strategy_id='__close_orphan__', tier-0 in executor.
    #   • flip_close    — current and target have OPPOSITE signs. Liquidates
    #                     the existing position fully. Tier-1 in executor.
    #                     Paired with flip_open below; flip_open must wait
    #                     for the close to fill (executor polls). Alpaca
    #                     bracket orders do not support auto-reverse so we
    #                     CANNOT submit a single oversize bracket — the
    #                     legacy delta path emitted exactly that and Alpaca
    #                     silently rejected it (or filled only the close
    #                     portion).
    #   • flip_open     — the paired new-direction open following flip_close.
    #                     Tier-2 (short) or tier-3 (long) in executor.
    broker = _load_broker_positions_usd()
    emissions = _classify_position_deltas(target_usd, broker, ticker_meta)
    flip_tickers = {tkr for tkr, _, kind in emissions if kind == 'flip_close'}
    logger.info(
        'regime_blended_sizer.sharpe_cadence: targets=%d, broker=%d, emissions=%d (flips=%d)',
        len(target_usd), len(broker), len(emissions), len(flip_tickers))

    # TradeJohn keep|cancel — proposed ONLY for new-exposure emissions
    # (delta, flip_open). Closes (orphan_close, flip_close) always execute:
    # orphan closes have no current signal to veto; flip closes are the
    # first leg of an atomic flip and a cancel on the matched flip_open
    # drops the flip_close below so the existing position is preserved.
    exposure_emissions = [(tkr, usd, kind) for (tkr, usd, kind) in emissions
                          if kind in ('delta', 'flip_open')]
    proposals = [{
        'ticker': tkr,
        'preliminary_size_usd': usd,
        'direction': 1 if usd >= 0 else -1,
        'contributions': [{'strategy_id': sid, 'attribution_weight': 1.0 / max(1, len(ticker_meta[tkr]['strategies']))}
                          for sid in ticker_meta[tkr]['strategies']],
        'bracket': {'entry_price': None, 'stop_loss': None, 'take_profit_1': None},
        'context': {'news_headlines': [], '30d_veto_history_for_ticker': 0,
                    'sector': '', 'hv30d': None},
    } for (tkr, usd, kind) in exposure_emissions]
    actions = {}
    if confirmer:
        try:
            actions = confirmer(proposals) or {}
        except Exception as e:
            logger.warning('regime_blended_sizer.sharpe_cadence: confirmer failed (%s); keeping all', e)

    canceled_deltas = {tkr for (tkr, _, kind) in emissions
                       if kind == 'delta' and (actions.get(tkr) or {}).get('action') == 'cancel'}
    canceled_flips = {tkr for tkr in flip_tickers
                      if (actions.get(tkr) or {}).get('action') == 'cancel'}
    emissions = [e for e in emissions
                 if not (e[2] == 'delta' and e[0] in canceled_deltas)
                 and not (e[2] in ('flip_close', 'flip_open') and e[0] in canceled_flips)]
    if canceled_flips:
        logger.info('regime_blended_sizer.sharpe_cadence: %d flips canceled by confirmer; existing positions preserved',
                    sorted(canceled_flips))

    # Emit orders. flip_close uses a dedicated strategy_id ('__flip_close__')
    # so the (run_date, strategy_id, ticker) UNIQUE constraint on
    # alpaca_submissions never collides with the matched flip_open row
    # (whose strategy_id is the real joined contributing IDs). The executor
    # detects flip_close by strategy_id, polls Alpaca until the close fills,
    # and only then submits the flip_open — Alpaca cannot transition
    # long↔short in a single bracket order.
    orders = []
    for tkr, usd, kind in emissions:
        dir_sign = 1 if usd >= 0 else -1
        if kind in ('orphan_close', 'flip_close'):
            bracket = {}     # forces close_only=True downstream
        else:
            bracket = _choose_bracket(ticker_meta[tkr].get('brackets', []),
                                      dir_sign, _ortho_groups, sharpe_by_strat)
        real_sid = '|'.join(sorted(set(ticker_meta[tkr]['strategies'])))[:120]
        if kind == 'flip_close':
            sid_out = '__flip_close__'
            out_target = 0.0
            out_current = broker.get(tkr, 0.0)
        elif kind == 'flip_open':
            sid_out = real_sid
            out_target = target_usd.get(tkr, 0.0)
            out_current = 0.0          # post-close perspective; matches the broker state the open will see
        else:
            sid_out = real_sid
            out_target = target_usd.get(tkr, 0.0)
            out_current = broker.get(tkr, 0.0)
        _order = {
            'ticker':                  tkr,
            'strategy_id':             sid_out,
            'direction':               'long' if dir_sign > 0 else 'short',
            'notional_usd':            abs(usd),
            'pct_nav':                 abs(usd) / nav,
            'shares':                  0,
            'entry':                   bracket.get('entry'),
            'stop':                    bracket.get('stop'),
            't1':                      bracket.get('t1'),
            't2':                      bracket.get('t2'),
            'kelly_final':             abs(usd) / nav,
            'ev':                      0.0,
            'p_t1':                    0.5,
            'source_mode':             'sharpe_cadence',
            'target_usd':              out_target,
            'current_usd':             out_current,
            'contributing_strategies': ticker_meta[tkr]['strategies'],
            'flip_action':             kind if kind in ('flip_close', 'flip_open') else None,
            'action':                  _derive_action(kind, out_current, out_target, dir_sign),
        }
        # SP-5.1a (G1 prong 1): the former SP-5.1c option_spec/instrument_class
        # attachment block lived here. Option contributors are partitioned out of
        # `active` so they never reach this equity loop — equity orders are now
        # byte-identical by construction (no option keys). Option orders are built
        # by _consolidate_option_orders below.
        orders.append(_order)

    # SP-5.1a (G1 prong 1 + G2): build option orders from the partitioned option
    # contributors, per-UNDERLYING and option-only. `scale` is the SAME λ×NAV/gross
    # rate the equity path computed (per-unit-weight USD parity). `equity_gross` is
    # the actual Σ|target_usd| (equity + any injected hedge) the option gross adds on
    # top of. When opt_active is empty (production today) this returns [] and `orders`
    # is byte-identical.
    _equity_gross = sum(abs(v) for v in target_usd.values())
    _opt_orders = _consolidate_option_orders(
        opt_active, weight_by_strat, sharpe_by_strat, scale, nav,
        min_cum_sharpe, account_state, _equity_gross)
    orders.extend(_opt_orders)

    # SP-5 Phase 1b (G4b): option orphan-close. The equity book logic above is
    # OCC-filtered (G1 prong 2) so it is structurally blind to held option legs —
    # nothing closes a held STRUCTURE whose option signal has disappeared. For each
    # held option underlying NOT among today's option targets, emit an option CLOSE
    # order. The executor's _route_mleg_close closes the actually-held legs per-leg
    # (structure-agnostic); the synthesized 'held_legs' spec survives OptionSpec.from_dict
    # (underlying present). Gated on OPENCLAW_OPTION_EXEC=1 so production today (gate off)
    # makes ZERO extra CLI calls — byte-identical inert.
    if os.environ.get('OPENCLAW_OPTION_EXEC') == '1':
        _opt_target_tickers = {o['ticker'] for o in _opt_orders}
        for _underlying, _legs in _held_option_underlyings().items():
            if _underlying in _opt_target_tickers:
                continue
            logger.info('regime_blended_sizer.sharpe_cadence: option orphan-close for %s '
                        '(%d held leg(s), no live option target)', _underlying, len(_legs))
            orders.append({
                'ticker':                  _underlying,
                'strategy_id':             '__close_option_orphan__',
                'direction':               'long',
                'notional_usd':            0.0,
                'pct_nav':                 0.0,
                'shares':                  0,
                'entry':                   None,
                'stop':                    None,
                't1':                      None,
                't2':                      None,
                'kelly_final':             0.0,
                'ev':                      0.0,
                'p_t1':                    0.5,
                'source_mode':             'sharpe_cadence',
                'target_usd':              0.0,
                'current_usd':             0.0,
                'contributing_strategies': ['__close_option_orphan__'],
                'flip_action':             None,
                'action':                  'CLOSE',
                'instrument_class':        'option',
                'option_spec':             {'underlying': _underlying, 'structure': 'held_legs'},
            })
    return orders


def _select_bracket(candidates: list[dict], dir_sign: int) -> dict:
    """Direction-leader bracket pick. From the contributing-signal pool,
    keep entries with matching direction, then pick the largest-weight
    bracket whose entry/stop/t1 are all populated *and finite* (NaN
    rejected — some strategies write Decimal('NaN') into execution_signals
    when their entry/stop math degenerates). Returns an empty dict if no
    usable bracket exists.
    """
    import math
    def _finite(x) -> bool:
        if x is None:
            return False
        try:
            return math.isfinite(float(x))
        except (TypeError, ValueError):
            return False

    if not candidates:
        return {}
    aligned = [b for b in candidates if b.get('direction') == dir_sign]
    usable  = [b for b in aligned
               if _finite(b.get('entry')) and _finite(b.get('stop'))
               and _finite(b.get('t1'))]
    if not usable:
        return {}
    usable.sort(key=lambda b: float(b.get('weight') or 0.0), reverse=True)
    return usable[0]


def _choose_bracket(candidates: list[dict], dir_sign: int,
                    ortho_groups: dict | None, sharpe_by_strat: dict) -> dict:
    """Gate decision for the bracket attached to an emission.
    OPENCLAW_STRATEGY_BRACKET_STACK ON + block substrate present -> stacked bracket
    (falls back to the legacy max-weight _select_bracket if stacking yields nothing).
    OFF (or no substrate) -> _select_bracket, byte-identical to legacy.
    Under ORTHO_SHADOW (and stacking OFF) it logs the would-be stacked bracket."""
    if ortho_groups and _ortho_enabled('OPENCLAW_STRATEGY_BRACKET_STACK'):
        from execution import bracket_stacking as _bs
        stacked = _bs.stacked_bracket(candidates, dir_sign,
                                      ortho_groups['block_map'], sharpe_by_strat)
        if stacked:
            return stacked
    selected = _select_bracket(candidates, dir_sign)
    if ortho_groups and _ortho_enabled('OPENCLAW_STRATEGY_ORTHO_SHADOW') \
            and not _ortho_enabled('OPENCLAW_STRATEGY_BRACKET_STACK'):
        try:
            from execution import bracket_stacking as _bs
            shadow_b = _bs.stacked_bracket(candidates, dir_sign,
                                           ortho_groups['block_map'], sharpe_by_strat)
            if shadow_b:
                logger.info(
                    'bracket_stack.shadow: selected(stop=%.2f t1=%.2f) vs '
                    'stacked(stop=%.2f t1=%.2f n=%d) %s',
                    float(selected.get('stop') or 0.0), float(selected.get('t1') or 0.0),
                    shadow_b['stop'], shadow_b['t1'], shadow_b['n_blocks'], shadow_b['why'])
        except Exception as _e:
            logger.warning('bracket_stack.shadow failed (%s)', _e)
    return selected
