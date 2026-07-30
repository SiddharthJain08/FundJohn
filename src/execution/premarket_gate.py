"""SP-6 Pre-Market Gate: approval verdict for overnight-held signals.

Loads COMPUTED signals (execution_signals WHERE lifecycle_state='COMPUTED'
AND target_date=today), reuses score_news_for_tickers + panic_score +
optional confirm_panic (Sonnet), assigns APPROVED or REJECTED verdict,
writes to signal_gate_verdicts, updates execution_signals.lifecycle_state.

FAIL-OPEN: FinBERT/Sonnet error → APPROVED + warn.

Gate-ran sentinel: writes a signal_gate_verdicts row with
gate_type='__gate_ran__' so downstream tasks (Task 8/12) can tell
"gate ran" from "gate crashed/never ran".

Gate type for per-signal rows: 'news_sentiment'.

Gated by: OPENCLAW_EOD_PREMARKET_GATE == '1' (default OFF)
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from ingestion.news_finbert_scorer import score_news_for_tickers  # noqa: E402
from sentiment.premarket_scorer import ScoreInputs, panic_score  # noqa: E402
from sentiment.sonnet_premarket_confirmer import (  # noqa: E402
    PremarketConfirmerInput,
    PremarketConfirmerResult,
    confirm_panic,
)

logger = logging.getLogger(__name__)

ENV_GATE = 'OPENCLAW_EOD_PREMARKET_GATE'
ENV_SAMEDAY = 'OPENCLAW_SAMEDAY_EXEC'
ENV_SAMEDAY_PROTECT = 'OPENCLAW_SAMEDAY_PREMARKET_PROTECT'

# panic_score threshold — mirrors the scanner's env default (35).
# Read at run_gate() call-time from OPENCLAW_PREMARKET_ADVISORY_THRESHOLD so
# gate and scanner stay in sync without a code change.
_DEFAULT_ADVISORY_THRESHOLD = 35.0


def _advisory_threshold() -> float:
    """Read threshold from env, defaulting to 35 (matches premarket scanner)."""
    return float(os.environ.get('OPENCLAW_PREMARKET_ADVISORY_THRESHOLD', _DEFAULT_ADVISORY_THRESHOLD))

# Gate type identifiers
GATE_TYPE_SIGNAL = 'news_sentiment'
GATE_TYPE_SENTINEL = '__gate_ran__'
# Same-day mode: the subject is a HELD POSITION, not a carried signal. Kept
# distinct so the audit trail never conflates "we vetoed tomorrow's intent"
# with "we vetoed a position we already own".
GATE_TYPE_HOLD = 'premarket_hold'


def sameday_protect_mode() -> bool:
    """True when the gate must protect the CURRENT BOOK rather than a carried
    signal register.

    Under same-day execution signals are COMPUTED at 15:00[T] and FILLED by
    15:55[T], so by 09:15[T+1] there is never a COMPUTED row with
    target_date=T+1 — the gate's subject set is ALWAYS empty and the operator's
    pre-market protective close silently does nothing (verified live
    2026-07-29: `REFUSING FLATTEN of 6 positions … holds=6`). The positions we
    actually carry overnight are yesterday's FILLED intent, i.e. the broker
    book, so that is what the gate scores in this mode.

    Escape hatch: OPENCLAW_SAMEDAY_PREMARKET_PROTECT=0 reverts to the
    signal-register subject set without a code change."""
    return (os.environ.get(ENV_SAMEDAY) == '1'
            and os.environ.get(ENV_SAMEDAY_PROTECT, '1') != '0')


def _load_held_subjects(broker_loader=None) -> list[dict]:
    """The overnight book as gate subjects: one pseudo-signal per held equity.

    `id` is None — these are POSITIONS, not signal rows, so the gate must not
    touch execution_signals.lifecycle_state for them (that column tracks the
    signal that opened the position and is already FILLED). The veto is
    recorded in signal_gate_verdicts and consumed by open_reconcile.

    Option/OCC legs are excluded: news-panic scoring on a contract symbol is
    meaningless, and the option lane owns its own closes."""
    if broker_loader is None:
        from execution.regime_blended_sizer import _load_broker_positions_usd
        broker_loader = _load_broker_positions_usd
    from execution.regime_blended_sizer import _is_occ_symbol
    try:
        book = broker_loader() or {}
    except Exception as exc:  # noqa: BLE001
        logger.error('premarket_gate: broker book unavailable (%s) — no held '
                     'subjects; the book is left UNPROTECTED this morning', exc)
        return []
    subjects: list[dict] = []
    for tkr, usd in sorted(book.items()):
        try:
            f = float(usd)
        except (TypeError, ValueError):
            continue
        if f == 0.0 or _is_occ_symbol(tkr) or '/' in tkr:
            continue
        subjects.append({
            'id': None,
            'strategy_id': None,
            'ticker': tkr,
            'direction': 'SHORT' if f < 0 else 'LONG',
            'position_size_pct': abs(f),
            'signal_date': None,
            'target_1': None,
            'stop_loss': None,
            'target_date': None,
            'computed_at': None,
        })
    return subjects


def is_enabled() -> bool:
    """True iff OPENCLAW_EOD_PREMARKET_GATE == '1'. Read at call-time so
    monkeypatch works in tests."""
    return os.environ.get(ENV_GATE) == '1'


def _get_db_conn():
    """Return a new psycopg2 DictCursor connection from POSTGRES_URI."""
    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        raise RuntimeError('POSTGRES_URI env var not set')
    return psycopg2.connect(uri, cursor_factory=psycopg2.extras.DictCursor)


def _load_carried_signals(cur, target_date) -> list[dict]:
    """Load all signals WHERE lifecycle_state='COMPUTED' AND target_date=today."""
    cur.execute(
        """
        SELECT id, strategy_id, signal_date, ticker, direction,
               position_size_pct, target_1, stop_loss, target_date, computed_at
          FROM execution_signals
         WHERE lifecycle_state = 'COMPUTED'
           AND target_date = %s
         ORDER BY signal_date ASC, ticker ASC
        """,
        (target_date,),
    )
    rows = cur.fetchall()
    return [dict(row) for row in rows]


def _write_gate_verdict(
    cur,
    signal_id: Optional[str],
    ticker: str,
    target_date,
    verdict: str,
    panic_score_val: float,
    news_count: int,
    severity: Optional[int],
    model: str,
    metadata: dict,
    actor: str,
    gate_type: str = GATE_TYPE_SIGNAL,
) -> None:
    """Insert one row into signal_gate_verdicts."""
    cur.execute(
        """
        INSERT INTO signal_gate_verdicts
            (signal_id, gate_type, ticker, target_date, verdict,
             panic_score, news_count, severity, model, metadata, actor, decided_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(signal_id) if signal_id is not None else None,
            gate_type,
            ticker,
            target_date,
            verdict,
            float(panic_score_val),
            news_count,
            severity,
            model,
            json.dumps(metadata),
            actor,
            datetime.now(timezone.utc),
        ),
    )


def _update_signal_lifecycle(
    cur,
    signal_id: str,
    lifecycle_state: str,
    approved_at: datetime,
    gate_verdict: dict,
) -> None:
    """Update execution_signals.lifecycle_state, approved_at, gate_verdict."""
    cur.execute(
        """
        UPDATE execution_signals
           SET lifecycle_state = %s,
               approved_at = %s,
               gate_verdict = %s
         WHERE id = %s
        """,
        (
            lifecycle_state,
            approved_at,
            json.dumps(gate_verdict),
            str(signal_id),
        ),
    )


def _write_gate_ran_sentinel(cur, target_date) -> None:
    """Write the __gate_ran__ sentinel row so Tasks 8/12 can confirm the gate ran.

    Contract: SELECT 1 FROM signal_gate_verdicts
              WHERE gate_type='__gate_ran__' AND target_date=<today>
    """
    cur.execute(
        """
        INSERT INTO signal_gate_verdicts
            (signal_id, gate_type, ticker, target_date, verdict,
             panic_score, news_count, severity, model, metadata, actor, decided_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            None,
            GATE_TYPE_SENTINEL,
            '__gate__',
            target_date,
            'ok',
            0.0,
            0,
            None,
            None,
            json.dumps({}),
            'premarket_gate',
            datetime.now(timezone.utc),
        ),
    )


def _post_premarket_alert(vetoed: list, target_date) -> None:
    """Announce held-position vetoes to #pre-market-alerts. Never fatal — the
    veto is already durable in signal_gate_verdicts before this runs."""
    helper = ROOT / 'src' / 'agent' / 'curators' / '_discord_webhook.js'
    if not helper.exists():
        return
    lines = '\n'.join(f'• **{t}** — panic {p:.0f}, severity {s}' for t, p, s in vetoed)
    text = (f'🛑 **Pre-market veto ({target_date})** — {len(vetoed)} held '
            f'position(s) flagged on news; the 9:25 reconcile will close them '
            f'at the open and the sizer will not re-open them today.\n{lines}')
    import subprocess
    js = ("const { postToChannel } = require(%r);"
          "let c=[];process.stdin.on('data',d=>c.push(d));"
          "process.stdin.on('end',async()=>{"
          "await postToChannel('botjohn','pre-market-alerts',"
          "Buffer.concat(c).toString('utf8'));});") % str(helper)
    try:
        subprocess.run(['node', '-e', js], input=text.encode('utf-8'),
                       capture_output=True, timeout=30, env={**os.environ})
    except Exception as exc:  # noqa: BLE001
        logger.warning('premarket veto alert failed to post: %s', exc)


def run_gate(conn=None, broker_loader=None) -> dict:
    """Main entry point.

    Loads COMPUTED signals for today, scores news sentiment, applies verdict
    logic (APPROVED / REJECTED), writes audit rows, updates lifecycle_state,
    writes the __gate_ran__ sentinel.

    Returns:
        {
            gate_ran: bool      — True only after the sentinel is written
            n_processed: int    — number of signals evaluated
            n_approved: int
            n_rejected: int
            errors: list[str]   — non-fatal errors (gate still marked ran)
        }

    Caller contract:
    - If conn is supplied, this function does NOT commit; the caller manages
      the transaction (test fixture rollback works correctly).
    - If conn is None (production), a new connection is opened, committed, and
      closed by run_gate.
    """
    if not is_enabled():
        logger.info('%s not set; gate disabled — returning no-op', ENV_GATE)
        return {
            'gate_ran': False,
            'n_processed': 0,
            'n_approved': 0,
            'n_rejected': 0,
            'errors': [],
        }

    own_conn = conn is None
    if own_conn:
        conn = _get_db_conn()

    try:
        cur = conn.cursor()
        target_date = datetime.now(timezone.utc).date()

        signals = _load_carried_signals(cur, target_date)
        logger.info(
            'Loaded %d COMPUTED signals for target_date=%s', len(signals), target_date
        )
        if sameday_protect_mode():
            # Union, not replacement: a COMPUTED-for-today row at 09:15 is an
            # anomaly in this mode (the 15:00 chain fills same-day), but if one
            # exists it still deserves a verdict.
            covered = {s['ticker'] for s in signals}
            held = [s for s in _load_held_subjects(broker_loader)
                    if s['ticker'] not in covered]
            signals = signals + held
            logger.info(
                'same-day protect mode: +%d held position(s) as gate subjects '
                '(the signal register is empty every morning in this mode)',
                len(held))

        errors: list[str] = []
        n_approved = 0
        n_rejected = 0
        vetoed_holds: list[tuple] = []

        if signals:
            tickers = list({s['ticker'] for s in signals})

            # ── Batch-fetch and FinBERT-score news (fail-open) ──────────────
            news_by_ticker: dict = {}
            try:
                window_start = datetime.now(timezone.utc) - timedelta(hours=18)
                news_scores = score_news_for_tickers(tickers, window_start)
                news_by_ticker = {ns['ticker']: ns for ns in news_scores}
            except Exception as exc:
                logger.warning(
                    'score_news_for_tickers error — fail-open (all signals APPROVED): %s',
                    exc,
                    exc_info=True,
                )
                errors.append(f'news_score_error: {str(exc)[:120]}')
                # news_by_ticker stays empty → panic_score=0 → APPROVED

            # ── Per-signal scoring + verdict ─────────────────────────────────
            approved_at = datetime.now(timezone.utc)

            for sig in signals:
                ticker = sig['ticker']
                signal_id = sig['id']

                news_data = news_by_ticker.get(ticker, {})
                news_count = news_data.get('news_count_24h', 0) or 0
                # Mirror the premarket scanner exactly: pass raw neg count, not ratio.
                # (run_premarket_scan.py line 189 does the same — panic_score clamps to [0,1])
                neg_count = news_data.get('news_finbert_neg', 0) or 0
                mean_score = news_data.get('news_mean_score') or 0.0
                top_headlines = news_data.get('news_top_headlines', []) or []
                evidence_uuids = news_data.get('evidence_uuids', []) or []

                score_inp = ScoreInputs(
                    news_count_window=news_count,
                    news_finbert_neg_ratio=float(neg_count),  # raw count, matches scanner
                    news_finbert_mean_score=float(mean_score),
                    social_post_count_window=0,
                    social_bear_ratio=0.0,
                )
                ps = panic_score(score_inp)

                # Ratio for confirmer display / audit (raw count / total)
                finbert_neg_ratio = (neg_count / news_count) if news_count > 0 else 0.0

                verdict = 'APPROVED'
                severity: Optional[int] = None
                model_label = 'rule_based_panic_score'
                advisory_threshold = _advisory_threshold()

                # Only REJECT on clear bearish evidence: panic≥threshold AND
                # confirmer returns bearish. Confirmer is always run past threshold
                # (treated as always-on; future OPENCLAW_PREMARKET_CONFIRMER gate
                # not wired here since we can only veto on confirmed evidence).
                # Confirmer errors → fail-open (APPROVED).
                if ps >= advisory_threshold:
                    model_label = 'sonnet_premarket_confirmer'
                    try:
                        headline_tuples: list[tuple[str, float, str]] = []
                        for i, h in enumerate(top_headlines[:3]):
                            uuid_str = evidence_uuids[i] if i < len(evidence_uuids) else ''
                            headline_tuples.append((h, 0.0, uuid_str))

                        conf_inp = PremarketConfirmerInput(
                            ticker=ticker,
                            held_qty=float(sig.get('position_size_pct') or 0.0),
                            panic_score=ps,
                            news_count=news_count,
                            finbert_neg_ratio=finbert_neg_ratio,
                            social_bear_ratio=0.0,
                            top_headlines=headline_tuples,
                        )
                        result: PremarketConfirmerResult = confirm_panic(conf_inp)

                        if result.verdict == 'llm_error':
                            logger.warning(
                                'confirm_panic LLM error for %s — fail-open APPROVED: %s',
                                ticker, result.rationale,
                            )
                            verdict = 'APPROVED'
                        elif result.verdict in ('bearish_news_driven', 'bearish_idiosyncratic'):
                            verdict = 'REJECTED'
                            severity = result.severity
                        else:
                            verdict = 'APPROVED'

                    except Exception as exc:
                        logger.warning(
                            'confirm_panic exception for %s — fail-open APPROVED: %s',
                            ticker, exc, exc_info=True,
                        )
                        verdict = 'APPROVED'
                        errors.append(f'confirmer_error_{ticker}: {str(exc)[:120]}')

                # ── Write verdict row ─────────────────────────────────────────
                gate_meta = {
                    'panic_score': float(ps),
                    'news_count': news_count,
                    'finbert_neg_ratio': float(finbert_neg_ratio),
                    'threshold': advisory_threshold,
                }
                # A held position (signal_id None) is audited under its own gate
                # type; a carried signal keeps the original one.
                is_hold = signal_id is None
                try:
                    _write_gate_verdict(
                        cur,
                        signal_id,
                        ticker,
                        target_date,
                        verdict,
                        ps,
                        news_count,
                        severity,
                        model_label,
                        gate_meta,
                        'premarket_gate',
                        gate_type=GATE_TYPE_HOLD if is_hold else GATE_TYPE_SIGNAL,
                    )
                except Exception as exc:
                    logger.error(
                        'write_gate_verdict failed for signal %s: %s', signal_id, exc
                    )
                    errors.append(f'write_verdict_error_{ticker}: {str(exc)[:120]}')
                    continue  # skip lifecycle update on DB error

                # ── Update signal lifecycle ───────────────────────────────────
                # HELD POSITIONS ARE NOT TRANSITIONED. The row that opened the
                # position is already FILLED; rewriting it to REJECTED would
                # corrupt the execution ledger and the P&L that hangs off it.
                # The veto reaches the broker through open_reconcile reading
                # this morning's premarket_hold verdicts.
                if not is_hold:
                    gate_verdict_obj = {
                        'verdict': verdict,
                        'panic_score': float(ps),
                        'news_count': news_count,
                        'severity': severity,
                        'model': model_label,
                    }
                    try:
                        _update_signal_lifecycle(
                            cur, signal_id, verdict, approved_at, gate_verdict_obj
                        )
                    except Exception as exc:
                        logger.error(
                            'update_signal_lifecycle failed for signal %s: %s',
                            signal_id, exc
                        )
                        errors.append(f'update_lifecycle_error_{ticker}: {str(exc)[:120]}')
                        continue

                if verdict == 'APPROVED':
                    n_approved += 1
                elif verdict == 'REJECTED':
                    n_rejected += 1

                logger.info(
                    '%s %s ticker=%s verdict=%s panic=%.1f',
                    'held-position' if is_hold else 'signal',
                    signal_id or '-', ticker, verdict, ps,
                )
                if is_hold and verdict == 'REJECTED':
                    vetoed_holds.append((ticker, ps, severity))
                    logger.warning(
                        'PREMARKET VETO: %s (panic=%.1f, severity=%s) — the 9:25 '
                        'reconcile will close this position at the open',
                        ticker, ps, severity)

        # ── Gate-ran sentinel (ALWAYS written on successful run) ──────────────
        _write_gate_ran_sentinel(cur, target_date)

        if vetoed_holds:
            # A veto closes a real position 10 minutes later; the operator runs
            # this book from Discord and should not learn it from a log file.
            # Isolated: the verdict rows are already written, and an alerting
            # failure reaching the outer handler would roll the whole gate back
            # and report gate_ran=False — a Discord outage must not be able to
            # un-veto a position.
            try:
                _post_premarket_alert(vetoed_holds, target_date)
            except Exception as exc:  # noqa: BLE001
                logger.warning('premarket veto alert failed: %s', exc)
                errors.append(f'veto_alert_error: {str(exc)[:120]}')

        # Commit only when we own the connection (production path).
        # Injected connections (tests) are managed by the caller.
        if own_conn:
            conn.commit()

        logger.info(
            'gate completed: n_processed=%d n_approved=%d n_rejected=%d errors=%d',
            len(signals) if signals else 0, n_approved, n_rejected, len(errors),
        )
        return {
            'gate_ran': True,
            'n_processed': len(signals) if signals else 0,
            'n_approved': n_approved,
            'n_rejected': n_rejected,
            'errors': errors,
        }

    except Exception as exc:
        logger.error('run_gate fatal error: %s', exc, exc_info=True)
        if own_conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return {
            'gate_ran': False,
            'n_processed': 0,
            'n_approved': 0,
            'n_rejected': 0,
            'errors': [f'fatal: {str(exc)[:200]}'],
        }

    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass
