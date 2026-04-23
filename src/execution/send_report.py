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

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.handoff import read_handoff  # noqa: E402

BOT_TOKEN = os.environ.get('DATABOT_TOKEN', '')
HEADERS = {
    'Authorization': f'Bot {BOT_TOKEN}',
    'Content-Type':  'application/json',
}


_GUILDS_CACHE: list | None = None

def _get_guilds() -> list:
    global _GUILDS_CACHE
    if _GUILDS_CACHE is not None:
        return _GUILDS_CACHE
    for attempt in range(5):
        r = requests.get('https://discord.com/api/v10/users/@me/guilds',
                         headers=HEADERS, timeout=10)
        if r.ok:
            _GUILDS_CACHE = r.json()
            return _GUILDS_CACHE
        if r.status_code == 429:
            wait = float(r.headers.get('Retry-After') or r.json().get('retry_after', 2))
            import time; time.sleep(min(wait + 0.5, 10))
            continue
        print(f'[send_report] guild list failed: {r.status_code}')
        break
    _GUILDS_CACHE = []
    return _GUILDS_CACHE


def _find_channel_id(name: str) -> str | None:
    for g in _get_guilds():
        for attempt in range(3):
            rc = requests.get(f"https://discord.com/api/v10/guilds/{g['id']}/channels",
                              headers=HEADERS, timeout=10)
            if rc.ok:
                for ch in rc.json():
                    if ch.get('name') == name and ch.get('type') == 0:
                        return ch['id']
                break
            if rc.status_code == 429:
                wait = float(rc.headers.get('Retry-After') or 2)
                import time; time.sleep(min(wait + 0.5, 10))
                continue
            break
    return None


def _post(channel_id: str, text: str) -> bool:
    remaining = text
    while remaining:
        chunk = remaining[:1900]
        remaining = remaining[1900:]
        r = requests.post(f'https://discord.com/api/v10/channels/{channel_id}/messages',
                          headers=HEADERS, json={'content': chunk}, timeout=10)
        if not r.ok:
            print(f'[send_report] post failed ({channel_id}): {r.status_code} {r.text[:200]}')
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


def _fmt_veto_digest(run_date: str, sized: dict) -> str:
    vetoed = sized.get('vetoed') or []
    if not vetoed:
        return f'🔕 **{run_date}** — no vetoed signals.'
    by_reason: dict[str, list[dict]] = {}
    for v in vetoed:
        by_reason.setdefault(v.get('reason') or 'unknown', []).append(v)
    lines = [f'🟥 **Veto digest — {run_date}** ({len(vetoed)} vetoed)', '']
    for reason, items in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        sample = ', '.join(f"{(i.get('ticker') or '?')}" for i in items[:10])
        more = f' … +{len(items) - 10} more' if len(items) > 10 else ''
        lines.append(f'• `{reason}` — {len(items)}: {sample}{more}')
    return '\n'.join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=str(date.today()))
    args = ap.parse_args()
    run_date = args.date

    sized = read_handoff(run_date, 'sized') or {}
    if not BOT_TOKEN:
        print('[send_report] DATABOT_TOKEN missing — printing to stdout only')
        print(_fmt_greenlist(run_date, sized))
        print('\n--- VETO DIGEST ---\n')
        print(_fmt_veto_digest(run_date, sized))
        return 0

    ch_signals = _find_channel_id('trade-signals')
    ch_reports = _find_channel_id('trade-reports')
    print(f'[send_report] channel lookup: trade-signals={ch_signals} trade-reports={ch_reports}')

    ok1 = _post(ch_signals, _fmt_greenlist(run_date, sized)) if ch_signals else False
    ok2 = _post(ch_reports, _fmt_veto_digest(run_date, sized)) if ch_reports else False
    if not ok1:
        print('[send_report] greenlist post skipped/failed')
    if not ok2:
        print('[send_report] veto-digest post skipped/failed')
    # Non-fatal: pipeline completes even if Discord is throttled. The data
    # is already written to the sized handoff file; operator can re-post.
    return 0


if __name__ == '__main__':
    sys.exit(main())
