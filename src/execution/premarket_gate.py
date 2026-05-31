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

# panic_score >= this threshold → run confirmer; reject only on bearish verdict
ADVISORY_THRESHOLD = 60.0

# Gate type identifiers
GATE_TYPE_SIGNAL = 'news_sentiment'
GATE_TYPE_SENTINEL = '__gate_ran__'


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


def run_gate(conn=None) -> dict:
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

        errors: list[str] = []
        n_approved = 0
        n_rejected = 0

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
                neg_count = news_data.get('news_finbert_neg', 0) or 0
                finbert_neg_ratio = (neg_count / news_count) if news_count > 0 else 0.0
                mean_score = news_data.get('news_mean_score') or 0.0
                top_headlines = news_data.get('news_top_headlines', []) or []
                evidence_uuids = news_data.get('evidence_uuids', []) or []

                score_inp = ScoreInputs(
                    news_count_window=news_count,
                    news_finbert_neg_ratio=finbert_neg_ratio,
                    news_finbert_mean_score=float(mean_score),
                    social_post_count_window=0,
                    social_bear_ratio=0.0,
                )
                ps = panic_score(score_inp)

                verdict = 'APPROVED'
                severity: Optional[int] = None
                model_label = 'rule_based_panic_score'

                # Only REJECT on clear bearish evidence: panic≥threshold AND
                # confirmer returns bearish. Confirmer is always run past threshold.
                # Confirmer errors → fail-open (APPROVED).
                if ps >= ADVISORY_THRESHOLD:
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
                    'threshold': ADVISORY_THRESHOLD,
                }
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
                    )
                except Exception as exc:
                    logger.error(
                        'write_gate_verdict failed for signal %s: %s', signal_id, exc
                    )
                    errors.append(f'write_verdict_error_{ticker}: {str(exc)[:120]}')
                    continue  # skip lifecycle update on DB error

                # ── Update signal lifecycle ───────────────────────────────────
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
                        'update_signal_lifecycle failed for signal %s: %s', signal_id, exc
                    )
                    errors.append(f'update_lifecycle_error_{ticker}: {str(exc)[:120]}')
                    continue

                if verdict == 'APPROVED':
                    n_approved += 1
                elif verdict == 'REJECTED':
                    n_rejected += 1

                logger.info(
                    'signal %s ticker=%s verdict=%s panic=%.1f',
                    signal_id, ticker, verdict, ps,
                )

        # ── Gate-ran sentinel (ALWAYS written on successful run) ──────────────
        _write_gate_ran_sentinel(cur, target_date)

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
