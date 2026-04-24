#!/usr/bin/env python3
"""
send_report.py — daily post-trade Discord report (Phase 2).

Reads the sized handoff written by trade_agent_llm.py (and acted on by
alpaca_executor.py) and posts two concise messages:

  • #trade-signals  — greenlist table (tickers that cleared the Kelly/EV
                      gate and went to Alpaca).
  • #trade-reports  — veto digest (tickers that didn't clear, with reasons).

Replaces the legacy per-strategy memo avalanche with one line per side.
No LLM, no markdown explosion — the dashboard is the source of truth for
drill-downs; Discord just mirrors the gist.

Usage:
    python3 src/execution/send_report.py --date YYYY-MM-DD
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import json
import requests

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.handoff import read_handoff  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))
except Exception:
    pass


def _get_webhook_urls(agent_id: str) -> dict:
    """Load the persisted webhook URLs for a persona from agent_registry.
    Posting via webhook bypasses bot role permissions, which is what was
    blocking earlier posts with a 403 Missing Permissions."""
    try:
        import psycopg2
        conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        cur = conn.cursor()
        cur.execute("SELECT webhook_urls FROM agent_registry WHERE id=%s", (agent_id,))
        row = cur.fetchone()
        conn.close()
        return (row[0] if row else {}) or {}
    except Exception as e:
        print(f'[send_report] webhook_urls load failed: {e}')
        return {}


def _post_webhook(webhook_url: str, text: str) -> bool:
    """Post via a Discord webhook URL. Splits at 1900 chars. Handles 429
    with Retry-After backoff. Returns True on all-chunks success."""
    import time
    remaining = text
    while remaining:
        chunk = remaining[:1900]
        remaining = remaining[1900:]
        for _attempt in range(5):
            try:
                r = requests.post(webhook_url, json={'content': chunk}, timeout=10)
            except Exception as e:
                print(f'[send_report] webhook post exception: {e}')
                return False
            if r.ok:
                break
            if r.status_code == 429:
                wait = 2.0
                try:
                    wait = float(r.headers.get('Retry-After') or r.json().get('retry_after') or 2)
                except Exception:
                    pass
                time.sleep(min(wait + 0.5, 10))
                continue
            print(f'[send_report] webhook post failed: {r.status_code} {r.text[:200]}')
            return False
        else:
            return False
    return True


def _fmt_greenlist(run_date: str, sized: dict) -> str:
    orders = sized.get('orders') or []
    regime = sized.get('regime') or '?'
    if not orders:
        return (f'✅ **{run_date}** — no actionable signals today '
                f'(regime={regime}). All signals failed the Kelly/EV gate.')
    lines = [f'🟢 **Greenlist — {run_date}** (regime={regime}, {len(orders)} orders)', '']
    header = f'{"Ticker":<8} {"Strategy":<28} {"Dir":<5} {"Entry":>9} {"Size%":>6} {"EV%":>7} {"p(T1)":>7}'
    lines.append('```')
    lines.append(header)
    lines.append('-' * len(header))
    for o in orders:
        ev = o.get('ev')
        p  = o.get('p_t1')
        lines.append(
            f"{(o.get('ticker') or '?'):<8} "
            f"{(o.get('strategy_id') or '?')[:28]:<28} "
            f"{(o.get('direction') or 'long')[:5]:<5} "
            f"{o.get('entry', 0) or 0:>9.2f} "
            f"{(o.get('pct_nav') or 0)*100:>6.2f} "
            f"{(ev*100) if ev is not None else 0:>+7.2f} "
            f"{(p*100) if p is not None else 0:>7.1f}"
        )
    lines.append('```')
    return '\n'.join(lines)


# Shared table layout for veto + overperformance digests. Both digests use
# the same 9-column schema so operators can scan the two side-by-side with
# identical column positions. Cells that don't apply to a given digest are
# rendered as an em-dash.
_DIGEST_HEADER = (
    f'{"Ticker":<8} {"Strategy":<28} {"Dir":<5} {"Classification":<28} '
    f'{"EV%":>8} {"Actual%":>8} {"Delta%":>8} {"p(T1)%":>7} {"Days":>5}'
)
_DIGEST_ROWS_MAX = 25


def _fmt_pct(v, sign: bool = True) -> str:
    if v is None:
        return f'{"—":>8}'
    fmt = f'{v*100:>+8.2f}' if sign else f'{v*100:>8.2f}'
    return fmt


def _fmt_int(v) -> str:
    if v is None or v == '':
        return f'{"—":>5}'
    return f'{int(v):>5}'


def _fmt_pt1(v) -> str:
    if v is None:
        return f'{"—":>7}'
    return f'{v*100:>7.1f}'


def _fmt_row(ticker, strategy, direction, classification, ev, actual, delta, p_t1, days) -> str:
    return (
        f'{(ticker or "?"):<8} '
        f'{(strategy or "?")[:28]:<28} '
        f'{(direction or "—")[:5]:<5} '
        f'{(classification or "—")[:28]:<28} '
        f'{_fmt_pct(ev)} '
        f'{_fmt_pct(actual)} '
        f'{_fmt_pct(delta)} '
        f'{_fmt_pt1(p_t1)} '
        f'{_fmt_int(days)}'
    )


def _fmt_overperformance_digest(run_date: str, overperf: list[dict]) -> str:
    """Yesterday's positions that beat their computed ev_gbm. Same 9-column
    schema as the veto digest — columns are in identical positions."""
    if not overperf:
        return f'📉 **{run_date}** — no overperformers from yesterday to report.'
    lines = [f'🚀 **Overperformance digest — d-1** ({len(overperf)} beat EV)', '']
    lines.append('```')
    lines.append(_DIGEST_HEADER)
    lines.append('-' * len(_DIGEST_HEADER))
    for o in overperf[:_DIGEST_ROWS_MAX]:
        actual = o.get('realized_pct') if o.get('realized_pct') is not None else o.get('unrealized_pct')
        # classification: "closed/target_1" if closed, else "open"
        cls = o.get('status') or '—'
        if o.get('close_reason'):
            cls = f'{cls}/{o["close_reason"]}'
        lines.append(_fmt_row(
            o.get('ticker'), o.get('strategy_id'), o.get('direction'),
            cls,
            o.get('ev_gbm'), actual, o.get('delta'),
            None,                      # p(T1) — not tracked post-entry
            o.get('days_held'),
        ))
    lines.append('```')
    if len(overperf) > _DIGEST_ROWS_MAX:
        lines.append(f'_+{len(overperf) - _DIGEST_ROWS_MAX} more — see structured handoff_')
    return '\n'.join(lines)


def _fmt_veto_digest(run_date: str, sized: dict) -> str:
    """Today's vetoed signals. Same 9-column schema as the overperformance
    digest — `Classification` holds the veto reason; `Actual%`, `Delta%`,
    and `Days` are always — since vetoes never become positions."""
    vetoed = sized.get('vetoed') or []
    if not vetoed:
        return f'🔕 **{run_date}** — no vetoed signals.'
    # Sort by abs(ev) desc — biggest-EV losers first (including prefilter
    # drops with the most-negative EV so operators see the worst at the top).
    ordered = sorted(
        vetoed,
        key=lambda v: -abs(float(v.get('ev') or 0)),
    )
    lines = [f'🟥 **Veto digest — {run_date}** ({len(vetoed)} vetoed)', '']
    lines.append('```')
    lines.append(_DIGEST_HEADER)
    lines.append('-' * len(_DIGEST_HEADER))
    for v in ordered[:_DIGEST_ROWS_MAX]:
        lines.append(_fmt_row(
            v.get('ticker'), v.get('strategy_id'), v.get('direction'),
            v.get('reason'),
            v.get('ev'),
            None,                      # Actual% — veto never entered
            None,                      # Delta% — no realized outcome
            v.get('p_t1'),
            None,                      # Days — veto never held
        ))
    lines.append('```')
    if len(vetoed) > _DIGEST_ROWS_MAX:
        lines.append(f'_+{len(vetoed) - _DIGEST_ROWS_MAX} more — see sized handoff_')
    return '\n'.join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=str(date.today()))
    args = ap.parse_args()
    run_date = args.date

    sized = read_handoff(run_date, 'sized') or {}

    # Webhooks from agent_registry (seeded by agent-personas initWebhooks).
    # Posting via webhook URL bypasses bot role permissions — the persistent
    # 403 from the DataBot/TradeDesk bot accounts goes away.
    hooks = _get_webhook_urls('tradedesk')
    wh_signals = hooks.get('trade-signals')
    wh_reports = hooks.get('trade-reports')
    print(f'[send_report] webhook lookup: trade-signals={"ok" if wh_signals else "missing"} '
          f'trade-reports={"ok" if wh_reports else "missing"}')

    if not wh_signals and not wh_reports:
        print('[send_report] no webhooks available — printing to stdout only')
        print(_fmt_greenlist(run_date, sized))
        print('\n--- VETO DIGEST ---\n')
        print(_fmt_veto_digest(run_date, sized))
        return 0

    ok1 = _post_webhook(wh_signals, _fmt_greenlist(run_date, sized)) if wh_signals else False
    ok2 = _post_webhook(wh_reports, _fmt_veto_digest(run_date, sized)) if wh_reports else False

    # Overperformance digest — read yesterday's overperformers from today's
    # structured handoff (the handoff builder populated this by
    # cross-referencing signal_pnl with yesterday's EVs).
    overperf = []
    try:
        structured = read_handoff(run_date, 'structured') or {}
        overperf = structured.get('yesterdays_overperformance') or []
    except Exception as e:
        print(f'[send_report] overperformance load skipped: {e}')
    ok3 = _post_webhook(wh_reports, _fmt_overperformance_digest(run_date, overperf)) if wh_reports else False

    if not ok1:
        print('[send_report] greenlist post skipped/failed')
    if not ok2:
        print('[send_report] veto-digest post skipped/failed')
    if not ok3:
        print('[send_report] overperformance-digest post skipped/failed')
    # Non-fatal: pipeline completes even if Discord is throttled. The data
    # is already written to the sized handoff file; operator can re-post.
    return 0


if __name__ == '__main__':
    sys.exit(main())
