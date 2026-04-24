#!/usr/bin/env python3
"""
send_report.py — daily post-trade Discord report (Phase 2).

Reads the sized handoff written by trade_agent_llm.py (and acted on by
alpaca_executor.py) and posts two concise messages:

  • #trade-signals  — greenlist table (tickers that cleared the Kelly/EV
                      gate and went to Alpaca).
  • #trade-reports  — combined underperformance + overperformance digest
                      for yesterday's positions (1σ-gated outcome outliers).

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


def _post_webhook_with_file(webhook_url: str, content: str, file_name: str, file_text: str) -> bool:
    """Single webhook POST with a short content message + a file attachment.
    Used to deliver the full d-1 outcomes tables (potentially hundreds of
    rows) without fan-out across multiple Discord messages."""
    import time
    for _attempt in range(5):
        try:
            r = requests.post(
                webhook_url,
                data={'payload_json': json.dumps({'content': content[:1900]})},
                files={'files[0]': (file_name, file_text.encode('utf-8'), 'text/plain')},
                timeout=30,
            )
        except Exception as e:
            print(f'[send_report] webhook-with-file exception: {e}')
            return False
        if r.ok:
            return True
        if r.status_code == 429:
            try:
                wait = float(r.headers.get('Retry-After') or r.json().get('retry_after') or 2)
            except Exception:
                wait = 2.0
            time.sleep(min(wait + 0.5, 10))
            continue
        print(f'[send_report] webhook-with-file failed: {r.status_code} {r.text[:200]}')
        return False
    return False


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


# Shared 9-column schema for under-/over-performance digests. Both come
# from the SAME data source (signal_pnl × yesterday's structured handoff)
# and carry the same fields — only the sign of sigma_delta differs. The
# schema is operator-facing: identical column positions so the two
# messages can be scanned side-by-side without remapping.
_DIGEST_HEADER = (
    f'{"Ticker":<8} {"Strategy":<28} {"Dir":<5} {"Status":<18} '
    f'{"EV%":>7} {"Actual%":>8} {"Delta%":>7} {"σΔ":>6} {"Days":>5}'
)
_DIGEST_ROWS_MAX = 25


def _fmt_pct(v) -> str:
    if v is None:
        return f'{"—":>7}'
    return f'{v*100:>+7.2f}'


def _fmt_pct8(v) -> str:
    if v is None:
        return f'{"—":>8}'
    return f'{v*100:>+8.2f}'


def _fmt_sigma(v) -> str:
    if v is None:
        return f'{"—":>6}'
    return f'{v:>+6.2f}'


def _fmt_int(v) -> str:
    if v is None or v == '':
        return f'{"—":>5}'
    return f'{int(v):>5}'


def _fmt_outlier_row(r: dict) -> str:
    cls = r.get('status') or '—'
    if r.get('close_reason'):
        cls = f'{cls}/{r["close_reason"]}'
    actual = r.get('realized_pct') if r.get('realized_pct') is not None else r.get('unrealized_pct')
    return (
        f'{(r.get("ticker") or "?"):<8} '
        f'{(r.get("strategy_id") or "?")[:28]:<28} '
        f'{(r.get("direction") or "—")[:5]:<5} '
        f'{cls[:18]:<18} '
        f'{_fmt_pct(r.get("ev_gbm"))} '
        f'{_fmt_pct8(actual)} '
        f'{_fmt_pct(r.get("delta"))} '
        f'{_fmt_sigma(r.get("sigma_delta"))} '
        f'{_fmt_int(r.get("days_held"))}'
    )


def _fmt_outlier_section(rows: list[dict], kind: str) -> list[str]:
    """Render one symmetric section (table) of the combined outcomes
    digest. Kind selects emoji + heading; table body is identical shape
    either way."""
    if kind == 'over':
        heading = f'🚀 Overperformance — {len(rows)} position(s) beat EV by ≥1σ'
    else:
        heading = f'🟥 Underperformance — {len(rows)} position(s) missed EV by ≥1σ'
    if not rows:
        return [heading, '(no positions cleared the 1σ gate)']
    return (
        [heading, _DIGEST_HEADER, '-' * len(_DIGEST_HEADER)]
        + [_fmt_outlier_row(r) for r in rows]
    )


def _fmt_outcomes_digest(run_date: str, overperf: list[dict], underperf: list[dict]) -> tuple[str, str]:
    """Single-message d-1 outcomes digest. Returns (summary, file_text):
    summary is a short Discord-embeddable recap with counts + top-5 from
    each bucket; file_text is the full stacked-tables body delivered as
    an attachment so operators get every row that cleared the 1σ gate
    without fragmenting the feed into dozens of 1900-char messages."""
    summary_lines = [f'🔭 **Daily outcomes — d-1 ({run_date})**', '']
    if not overperf and not underperf:
        summary_lines.append('_No positions cleared the 1σ gate in either direction._')
        return ('\n'.join(summary_lines), '')

    def _top5(rows: list[dict]) -> str:
        if not rows:
            return '_none_'
        return ', '.join(
            f"{r.get('ticker')}/{(r.get('strategy_id') or '')[:18]} ({(r.get('sigma_delta') or 0):+.2f}σ)"
            for r in rows[:5]
        )

    summary_lines.append(f'🟥 **Underperformance** — {len(underperf)} · top 5: {_top5(underperf)}')
    summary_lines.append(f'🚀 **Overperformance**  — {len(overperf)} · top 5: {_top5(overperf)}')
    summary_lines.append('')
    summary_lines.append('_Full tables attached._')

    # File body: both full tables, stacked. Plain monospaced text so
    # Discord's attachment preview renders it inline.
    file_lines = [f'Daily outcomes — d-1 ({run_date})', '=' * 60, '']
    file_lines += _fmt_outlier_section(underperf, 'under')
    file_lines += ['', '']
    file_lines += _fmt_outlier_section(overperf, 'over')

    return ('\n'.join(summary_lines), '\n'.join(file_lines))


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

    # Yesterday's performance outliers — same source (signal_pnl × d-1
    # structured handoff), symmetric 1σ gate, rendered with identical
    # table layouts so operators can scan the two messages side-by-side.
    overperf: list = []
    underperf: list = []
    try:
        structured = read_handoff(run_date, 'structured') or {}
        overperf  = structured.get('yesterdays_overperformance')  or []
        underperf = structured.get('yesterdays_underperformance') or []
    except Exception as e:
        print(f'[send_report] outlier load skipped: {e}')

    summary, file_text = _fmt_outcomes_digest(run_date, overperf, underperf)

    if not wh_signals and not wh_reports:
        print('[send_report] no webhooks available — printing to stdout only')
        print(_fmt_greenlist(run_date, sized))
        print()
        print(summary)
        if file_text:
            print('\n--- ATTACHMENT (outcomes_d-1.txt) ---\n')
            print(file_text)
        return 0

    ok1 = _post_webhook(wh_signals, _fmt_greenlist(run_date, sized)) if wh_signals else False

    if wh_reports:
        if file_text:
            ok2 = _post_webhook_with_file(
                wh_reports, summary,
                f'outcomes_d-1_{run_date}.txt', file_text,
            )
        else:
            # No outliers — summary is short, no attachment needed.
            ok2 = _post_webhook(wh_reports, summary)
    else:
        ok2 = False

    if not ok1: print('[send_report] greenlist post skipped/failed')
    if not ok2: print('[send_report] outcomes-digest post skipped/failed')
    # Non-fatal: pipeline completes even if Discord is throttled. Data is
    # persisted in the sized / structured handoffs; operator can re-post.
    return 0


if __name__ == '__main__':
    sys.exit(main())
