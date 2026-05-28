"""Pre-market sentiment panic scanner — sidecar entry point.

Two-shot daily: timer fires at 07:30 ET and 09:00 ET on trading days.

Gate hierarchy (all default-OFF):
  OPENCLAW_PREMARKET_SCAN=1       -> master; service refuses to run without this
  OPENCLAW_PREMARKET_CONFIRMER=1  -> call Sonnet on rule-flagged tickers
  OPENCLAW_PREMARKET_AUTOCLOSE=1  -> auto-flatten on strict Sonnet verdict
                                     (requires CONFIRMER=1; startup raises otherwise)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.request
import uuid as _uuid_mod
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg2
from psycopg2.extras import execute_values

from src.pipeline.premarket_helpers import (
    is_trading_day_in_et,
    load_open_equity_positions,
    resolve_premarket_webhook,
)
from src.ingestion.news_finbert_scorer import score_news_for_tickers
from src.sentiment.premarket_scorer import ScoreInputs, panic_score
from src.sentiment.sonnet_premarket_confirmer import (
    PANIC_VERDICTS,
    PremarketConfirmerInput,
    confirm_panic,
)
from src.execution.regime_liquidator import close_subset


log = logging.getLogger(__name__)
_ET = ZoneInfo('America/New_York')

STRICT_AUTOCLOSE_VERDICTS = {'bearish_news_driven', 'bearish_idiosyncratic'}


class GateConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScanConfig:
    scan_enabled: bool
    confirmer_enabled: bool
    autoclose_enabled: bool
    advisory_threshold: float
    autoclose_min_severity: int
    max_tickers_per_scan: int
    confirmer_budget_usd: float

    @classmethod
    def from_env(cls) -> 'ScanConfig':
        scan = os.environ.get('OPENCLAW_PREMARKET_SCAN', '0') == '1'
        conf = os.environ.get('OPENCLAW_PREMARKET_CONFIRMER', '0') == '1'
        auto = os.environ.get('OPENCLAW_PREMARKET_AUTOCLOSE', '0') == '1'
        if auto and not conf:
            raise GateConfigError(
                'OPENCLAW_PREMARKET_AUTOCLOSE=1 requires OPENCLAW_PREMARKET_CONFIRMER=1'
            )
        return cls(
            scan_enabled=scan,
            confirmer_enabled=conf,
            autoclose_enabled=auto,
            advisory_threshold=float(os.environ.get(
                'OPENCLAW_PREMARKET_ADVISORY_THRESHOLD', '35')),
            autoclose_min_severity=int(os.environ.get(
                'OPENCLAW_PREMARKET_AUTOCLOSE_MIN_SEVERITY', '4')),
            max_tickers_per_scan=int(os.environ.get(
                'OPENCLAW_PREMARKET_MAX_TICKERS_PER_SCAN', '25')),
            confirmer_budget_usd=float(os.environ.get(
                'OPENCLAW_PREMARKET_CONFIRMER_BUDGET_USD', '0.50')),
        )


def _premarket_window_start_utc(scan_ts: datetime) -> datetime:
    """Window starts at the prior trading day's 18:00 ET. For a 07:30 ET scan
    on 2026-05-28, the start is 2026-05-27 18:00 ET == 22:00 UTC.
    """
    scan_et = scan_ts.astimezone(_ET)
    prior_et = (scan_et - timedelta(days=1)).date()
    start_et = datetime.combine(prior_et, time(18, 0), tzinfo=_ET)
    return start_et.astimezone(timezone.utc)


def _filter_uuid_strs(items: list | None) -> list[str] | None:
    """Filter a list to only valid UUID-formatted strings.

    market_news.uuid is TEXT, so some values (e.g. Alpaca news ids like
    'alpaca-news-12345') are not valid UUIDs. Passing non-UUID strings to a
    UUID[] column causes the entire INSERT to fail. We drop them here.
    Returns None when the filtered list is empty, so the DB column stays NULL.
    """
    if not items:
        return None
    out = []
    for s in items:
        try:
            _uuid_mod.UUID(str(s))
            out.append(str(s))
        except (ValueError, TypeError):
            continue
    return out or None


def _persist_alert_rows(rows: list[dict]) -> None:
    if not rows:
        return
    dsn = os.environ['POSTGRES_URI']
    cols = (
        'scan_ts', 'scan_label', 'trading_day', 'ticker', 'held_qty',
        'avg_entry_price',
        'news_count_window', 'news_finbert_neg_ratio',
        'news_finbert_mean_score',
        'social_post_count_window', 'social_bear_ratio',
        'panic_score', 'advisory_fired',
        'sonnet_verdict', 'sonnet_severity', 'sonnet_rationale',
        'sonnet_evidence_uuids', 'sonnet_cost_usd',
        'autoclose_fired', 'autoclose_liquidation_id',
    )
    # Filter sonnet_evidence_uuids: market_news.uuid is text, so non-UUID strings
    # (e.g. Alpaca news ids) are dropped to avoid UUID[] cast failure on INSERT.
    for r in rows:
        r['sonnet_evidence_uuids'] = _filter_uuid_strs(r.get('sonnet_evidence_uuids'))
    values = [tuple(r.get(c) for c in cols) for r in rows]
    placeholder = '(' + ','.join(['%s'] * len(cols)) + ')'
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        execute_values(
            cur,
            f'INSERT INTO premarket_panic_alerts ({",".join(cols)}) VALUES %s',
            values, template=placeholder,
        )
        conn.commit()


def _format_discord_summary(rows: list[dict], scan_label: str) -> str:
    fired = [r for r in rows if r.get('advisory_fired')]
    if not fired:
        return ''  # silent on calm mornings
    lines = [f'**Pre-market panic scan — {scan_label} ET**']
    for r in fired:
        ticker = r['ticker']
        score = r['panic_score']
        qty = r['held_qty']
        verdict = r.get('sonnet_verdict') or 'rules-only'
        sev = r.get('sonnet_severity')
        rationale = (r.get('sonnet_rationale') or '').strip()
        sev_str = f' sev={sev}' if sev is not None else ''
        head = f'• `{ticker}` qty={qty:+g} score={score:.0f} verdict={verdict}{sev_str}'
        lines.append(head)
        if rationale:
            lines.append(f'    {rationale[:300]}')
        if r.get('autoclose_fired'):
            lines.append('    AUTO-CLOSE submitted.')
    return '\n'.join(lines)


def _post_discord(url: str, content: str) -> None:
    if not url or not content:
        return
    req = urllib.request.Request(
        url, data=json.dumps({'content': content}).encode(),
        headers={'Content-Type': 'application/json'},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # noqa: BLE001 — never fail the scan over Discord
        log.warning('discord post failed: %s', e)


def _evaluate_ticker(position: dict, cfg: ScanConfig, scan_ts: datetime,
                     scan_label: str, window_start: datetime) -> dict:
    ticker = position['symbol']
    news_rows = score_news_for_tickers([ticker], window_start)
    n = news_rows[0] if news_rows else None

    inputs = ScoreInputs(
        news_count_window=int(n['news_count_24h']) if n else 0,
        news_finbert_neg_ratio=float(n['news_finbert_neg']) if n else 0.0,
        news_finbert_mean_score=float(n['news_mean_score']) if n else 0.0,
        social_post_count_window=0,    # MVP: social pulled in handoff future iteration
        social_bear_ratio=0.0,
    )
    score = panic_score(inputs)
    advisory = score >= cfg.advisory_threshold

    row: dict = {
        'scan_ts': scan_ts,
        'scan_label': scan_label,
        'trading_day': scan_ts.astimezone(_ET).date(),
        'ticker': ticker,
        'held_qty': position['qty'],
        'avg_entry_price': position.get('avg_entry_price'),
        'news_count_window': inputs.news_count_window,
        'news_finbert_neg_ratio': inputs.news_finbert_neg_ratio,
        'news_finbert_mean_score': inputs.news_finbert_mean_score,
        'social_post_count_window': 0,
        'social_bear_ratio': 0.0,
        'panic_score': score,
        'advisory_fired': advisory,
        'sonnet_verdict': None,
        'sonnet_severity': None,
        'sonnet_rationale': None,
        'sonnet_evidence_uuids': None,
        'sonnet_cost_usd': None,
        'autoclose_fired': False,
        'autoclose_liquidation_id': None,
    }

    if advisory and cfg.confirmer_enabled and n is not None:
        top = list(zip(
            n.get('news_top_headlines', [])[:5],
            [inputs.news_finbert_mean_score] * 5,
            (n.get('evidence_uuids') or [])[:5],
        ))
        result = confirm_panic(
            PremarketConfirmerInput(
                ticker=ticker, held_qty=position['qty'], panic_score=score,
                news_count=inputs.news_count_window,
                finbert_neg_ratio=inputs.news_finbert_neg_ratio,
                social_bear_ratio=0.0,
                top_headlines=top,
            ),
            max_budget_usd=cfg.confirmer_budget_usd,
        )
        row['sonnet_verdict'] = result.verdict
        row['sonnet_severity'] = result.severity
        row['sonnet_rationale'] = result.rationale
        row['sonnet_evidence_uuids'] = result.evidence_uuids or None
        row['sonnet_cost_usd'] = result.cost_usd
    return row


def _should_autoclose(row: dict, cfg: ScanConfig) -> bool:
    return (
        cfg.autoclose_enabled
        and row['sonnet_verdict'] in STRICT_AUTOCLOSE_VERDICTS
        and row.get('sonnet_severity') is not None
        and row['sonnet_severity'] >= cfg.autoclose_min_severity
    )


def run_scan(scan_label: str, ticker_override: list[str] | None = None) -> int:
    cfg = ScanConfig.from_env()
    if not cfg.scan_enabled:
        log.info('OPENCLAW_PREMARKET_SCAN=0; exiting silently')
        return 0
    if not is_trading_day_in_et():
        log.info('not a trading day in ET; exiting silently')
        return 0

    positions = load_open_equity_positions()
    if ticker_override:
        positions = [p for p in positions if p['symbol'] in set(ticker_override)]
    if not positions:
        log.info('no open equity positions; exiting')
        return 0

    if len(positions) > cfg.max_tickers_per_scan:
        log.warning('truncating %d positions to max %d',
                    len(positions), cfg.max_tickers_per_scan)
        positions = positions[:cfg.max_tickers_per_scan]

    scan_ts = datetime.now(timezone.utc)
    window_start = _premarket_window_start_utc(scan_ts)
    rows = [
        _evaluate_ticker(p, cfg, scan_ts, scan_label, window_start)
        for p in positions
    ]

    # Auto-close gate
    if cfg.autoclose_enabled:
        flagged = [r for r in rows if _should_autoclose(r, cfg)]
        if flagged:
            outcomes = close_subset(
                [r['ticker'] for r in flagged], reason='PREMARKET_PANIC',
            )
            by_ticker = {o['ticker']: o for o in outcomes}
            for r in flagged:
                o = by_ticker.get(r['ticker'], {})
                r['autoclose_fired'] = o.get('status') in {'filled', 'pending', 'accepted'}
                r['autoclose_liquidation_id'] = o.get('liquidation_id')

    _persist_alert_rows(rows)

    webhook = resolve_premarket_webhook()
    summary = _format_discord_summary(rows, scan_label)
    if webhook and summary:
        _post_discord(webhook, summary)

    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument('--scan-label', required=True, choices=['07:30', '09:00'])
    parser.add_argument('--tickers', nargs='*',
                        help='override broker-position lookup for debugging')
    args = parser.parse_args(argv)

    try:
        cfg = ScanConfig.from_env()
    except GateConfigError as e:
        log.error('gate config: %s', e)
        return 2

    return run_scan(args.scan_label, ticker_override=args.tickers)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
