#!/usr/bin/env python3
"""
send_report.py — daily post-trade Discord report (Phase 2).

Reads the sized handoff written by regime_blended_sizer_live (and acted on
by alpaca_executor.py) and posts two concise messages:

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
import psycopg2
import psycopg2.extras
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
    # `regime` in sized handoff is the full dict {state, stress, scale, vix_level, ...}
    # but the Discord message only wants the state string.
    regime_raw = sized.get('regime') or '?'
    regime = regime_raw.get('state', '?') if isinstance(regime_raw, dict) else regime_raw
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


def _fmt_pct(v, width: int = 7) -> str:
    if v is None:
        return f'{"—":>{width}}'
    return f'{v*100:>+{width}.2f}'


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
        f'{_fmt_pct(r.get("ev_gbm"), 7)} '
        f'{_fmt_pct(actual, 8)} '
        f'{_fmt_pct(r.get("delta"), 7)} '
        f'{_fmt_sigma(r.get("sigma_delta"))} '
        f'{_fmt_int(r.get("days_held"))}'
    )


def _sigma_distribution(rows: list[dict]) -> str:
    """Bin rows by |σΔ| magnitude band for the summary block — gives
    operators a quick read of how extreme the outliers are without
    scanning the full attached table."""
    bands = [(2.0, 3.0), (3.0, 5.0), (5.0, 10.0), (10.0, float('inf'))]
    counts = [0, 0, 0, 0]
    for r in rows:
        s = abs(float(r.get('sigma_delta') or 0))
        for i, (lo, hi) in enumerate(bands):
            if lo <= s < hi:
                counts[i] += 1
                break
    labels = ['2–3σ', '3–5σ', '5–10σ', '≥10σ']
    return ' · '.join(f'{l}: **{c}**' for l, c in zip(labels, counts) if c > 0) or '_none_'


def _fmt_outlier_section(rows: list[dict], kind: str, gate: float) -> list[str]:
    """Render one symmetric section (table) of the combined outcomes
    digest. Kind selects emoji + heading; table body is identical shape
    either way."""
    if kind == 'over':
        heading = f'🚀 Overperformance — {len(rows)} position(s) beat EV by ≥{gate:.1f}σ'
    else:
        heading = f'🟥 Underperformance — {len(rows)} position(s) missed EV by ≥{gate:.1f}σ'
    if not rows:
        return [heading, f'(no positions cleared the {gate:.1f}σ gate)']
    return (
        [heading, _DIGEST_HEADER, '-' * len(_DIGEST_HEADER)]
        + [_fmt_outlier_row(r) for r in rows]
    )


_OUE_EMOJI = {'over': '🚀', 'under': '🟥', 'expected': '🟢'}


def _fmt_money(v) -> str:
    """Signed, comma-grouped whole-dollar string. '—' when unknown."""
    if v is None:
        return '—'
    sign = '-' if v < 0 else '+'
    return f'{sign}${abs(v):,.0f}'


def _aggregate_closed_positions(rows: list[dict]) -> dict:
    """Pure aggregation of closed-position legs into by-ticker + overall
    stats. Each input row is one (ticker, strategy) close leg (see the
    row contract in tests/test_send_report_closed_digest.py).

    Dollar P&L is estimated at the TICKER level — notional × mean realized
    % — because the broker nets one position per ticker, so per-strategy
    dollar attribution isn't available. Percentage / win / OUE / reason
    stats stay per-leg (per strategy)."""
    oue = {'over': 0, 'under': 0, 'expected': 0}
    by_reason: dict[str, int] = {}
    wins = losses = flat = 0
    realized_sum = 0.0
    days_sum = 0.0
    groups: dict[str, list[dict]] = {}

    for r in rows:
        rp = float(r.get('realized_pct') or 0.0)
        realized_sum += rp
        days_sum += float(r.get('days_held') or 0)
        if rp > 0:
            wins += 1
        elif rp < 0:
            losses += 1
        else:
            flat += 1
        kind = r.get('oue_kind')
        if kind in oue:
            oue[kind] += 1
        reason = r.get('close_reason') or 'unknown'
        by_reason[reason] = by_reason.get(reason, 0) + 1
        groups.setdefault(r.get('ticker') or '?', []).append(r)

    total = len(rows)
    tickers: list[dict] = []
    net_dollar = 0.0
    dollar_known = 0
    for tk, legs in groups.items():
        t_avg = sum(float(l.get('realized_pct') or 0.0) for l in legs) / len(legs)
        # Notional is a ticker property repeated on every leg; take the
        # first non-None (the broker holds one netted position per ticker).
        notional = next(
            (float(l['ticker_notional_usd']) for l in legs
             if l.get('ticker_notional_usd') is not None),
            None,
        )
        est = notional * t_avg if notional is not None else None
        if est is not None:
            net_dollar += est
            dollar_known += 1
        tickers.append({
            'ticker': tk,
            'n': len(legs),
            'avg_realized_pct': t_avg,
            'notional_usd': notional,
            'est_dollar_pnl': est,
            'legs': legs,
        })

    tickers.sort(key=lambda t: (-t['avg_realized_pct'], t['ticker']))
    return {
        'total_closed': total,
        'n_tickers': len(groups),
        'wins': wins,
        'losses': losses,
        'flat': flat,
        'win_rate': (wins / total) if total else 0.0,
        'avg_realized_pct': (realized_sum / total) if total else 0.0,
        'avg_days_held': (days_sum / total) if total else 0.0,
        'net_dollar_pnl': net_dollar,
        'dollar_known_tickers': dollar_known,
        'by_reason': by_reason,
        'oue': oue,
        'tickers': tickers,
    }


def _fmt_closed_positions_digest(run_date: str, rows: list[dict]) -> tuple[str, str]:
    """Build the #trade-reports digest of every position that closed in
    this cycle, grouped by ticker with per-strategy legs. Returns
    (summary, file_text); file_text is '' when nothing closed.

    Reads nothing — pure function over the enriched leg rows produced by
    `_load_closed_positions`. This is why the report is immune to the
    persisted-oue_kind bug: it derives its own stats from realized P&L."""
    agg = _aggregate_closed_positions(rows)
    total = agg['total_closed']
    if total == 0:
        return (f'📊 **Closed positions — {run_date}** · '
                f'no positions closed in this cycle.', '')

    oue = agg['oue']
    reason_str = ' · '.join(
        f'{k}: **{v}**'
        for k, v in sorted(agg['by_reason'].items(), key=lambda kv: -kv[1])
    )
    priced = agg['dollar_known_tickers']
    price_note = '' if priced == agg['n_tickers'] else f' ({priced}/{agg["n_tickers"]} priced)'

    tickers = agg['tickers']
    winners = [t for t in tickers if t['avg_realized_pct'] > 0][:3]
    losers = [t for t in reversed(tickers) if t['avg_realized_pct'] < 0][:3]

    def _brief(t: dict) -> str:
        return (f"`{t['ticker']}` {t['avg_realized_pct'] * 100:+.2f}% "
                f"({_fmt_money(t['est_dollar_pnl'])})")

    summary_lines = [
        f'📊 **Closed positions — {run_date}** · **{total}** closed across '
        f'**{agg["n_tickers"]}** ticker(s) · est. net {_fmt_money(agg["net_dollar_pnl"])}{price_note}',
        '',
        f'Realized: avg **{agg["avg_realized_pct"] * 100:+.2f}%** · '
        f'win rate **{agg["win_rate"] * 100:.0f}%** ({agg["wins"]}W/{agg["losses"]}L) · '
        f'avg hold **{agg["avg_days_held"]:.1f}**d',
        f'By reason: {reason_str}',
        f'OUE (live): {_OUE_EMOJI["over"]} over **{oue["over"]}** · '
        f'{_OUE_EMOJI["under"]} under **{oue["under"]}** · '
        f'{_OUE_EMOJI["expected"]} expected **{oue["expected"]}**',
        '',
        f'Top winners: {", ".join(_brief(t) for t in winners) if winners else "_none_"}',
        f'Top losers: {", ".join(_brief(t) for t in losers) if losers else "_none_"}',
        '',
        '_Full by-ticker breakdown attached._',
    ]

    file_lines = [
        f'Closed positions — {run_date}',
        '=' * 64,
        f'Total closed:   {total}  across {agg["n_tickers"]} ticker(s)',
        f'Win/Loss:       {agg["wins"]}W / {agg["losses"]}L'
        + (f' / {agg["flat"]} flat' if agg['flat'] else '')
        + f'  (win rate {agg["win_rate"] * 100:.0f}%)',
        f'Avg realized:   {agg["avg_realized_pct"] * 100:+.2f}%',
        f'Avg hold:       {agg["avg_days_held"]:.1f} days',
        f'Est. net P&L:   {_fmt_money(agg["net_dollar_pnl"])}{price_note}',
        'By reason:      ' + ', '.join(
            f'{k}={v}' for k, v in sorted(agg['by_reason'].items(), key=lambda kv: -kv[1])),
        f'OUE (live):     over={oue["over"]}  under={oue["under"]}  expected={oue["expected"]}',
        '',
        'Dollar P&L is a ticker-level estimate (notional x mean realized %);',
        'the broker nets one position per ticker, so it is not per-strategy.',
        '',
    ]
    leg_header = (f'  {"strategy":<32} {"dir":<5} {"realized":>9} {"days":>5} '
                  f'{"reason":<12} {"OUE":<9} {"sigma":>7}')
    for t in tickers:
        file_lines.append('')
        file_lines.append(
            f'── {t["ticker"]}  ·  {t["n"]} close(s)  ·  avg {t["avg_realized_pct"] * 100:+.2f}%  ·  '
            f'est {_fmt_money(t["est_dollar_pnl"])}'
            + (f'  (notional {_fmt_money(t["notional_usd"])})' if t['notional_usd'] is not None else '')
            + ' ──'
        )
        file_lines.append(leg_header)
        file_lines.append('  ' + '-' * (len(leg_header) - 2))
        for leg in t['legs']:
            sd = leg.get('oue_sigma_delta')
            file_lines.append(
                f'  {(leg.get("strategy_id") or "")[:32]:<32} '
                f'{(leg.get("direction") or "")[:5]:<5} '
                f'{float(leg.get("realized_pct") or 0.0) * 100:>+8.2f}% '
                f'{int(leg.get("days_held") or 0):>5} '
                f'{(leg.get("close_reason") or ""):<12} '
                f'{(leg.get("oue_kind") or ""):<9} '
                + (f'{float(sd):>+7.2f}' if sd is not None else f'{"—":>7}')
            )
    return ('\n'.join(summary_lines), '\n'.join(file_lines))


def _load_closed_positions(run_date: str) -> list[dict]:
    """Fetch every position that closed on run_date directly from
    signal_pnl (the committed source of truth) — independent of the
    persisted execution_signals.oue_kind field, which is why this
    survives the classifier-ordering bug.

    Each leg is enriched with (a) a live OUE classification recomputed
    from the signal's handoff EV, and (b) a ticker-level broker notional
    (latest filled submission for that ticker on/before the close)."""
    import os
    uri = os.environ.get('POSTGRES_URI', '')
    if not uri:
        return []
    from execution.oue_classifier import _load_signal_ev, classify, _get_sigma_gate
    try:
        with psycopg2.connect(uri) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                sigma_gate = _get_sigma_gate(cur)
                cur.execute("""
                    SELECT es.id AS signal_id, es.ticker, es.strategy_id,
                           es.direction, es.signal_date,
                           sp.realized_pnl_pct, sp.days_held, sp.close_reason,
                           sub.notional_usd
                      FROM execution_signals es
                      JOIN LATERAL (
                          -- rolled_continuation rows are roll segments of an
                          -- ongoing position (SP-6 D1), not trades; excluded
                          -- from report stats (NULL-safe). Filtering here drops
                          -- rolls uniformly from both by-ticker stats and the
                          -- close_reason buckets downstream.
                          SELECT realized_pnl_pct, days_held, close_reason, pnl_date
                            FROM signal_pnl
                           WHERE signal_id = es.id
                             AND status = 'closed'
                             AND realized_pnl_pct IS NOT NULL
                             AND close_reason IS DISTINCT FROM 'rolled_continuation'
                             AND pnl_date::date = %s
                           ORDER BY pnl_date DESC
                           LIMIT 1
                      ) sp ON TRUE
                      LEFT JOIN LATERAL (
                          -- Broker nets one position per ticker; submission
                          -- strategy_id is a pipe-bundle, so match ticker only.
                          SELECT notional_usd
                            FROM alpaca_submissions
                           WHERE ticker = es.ticker
                             AND run_date <= sp.pnl_date
                             AND notional_usd IS NOT NULL
                           ORDER BY run_date DESC
                           LIMIT 1
                      ) sub ON TRUE
                     WHERE es.status = 'closed'
                     ORDER BY es.ticker, es.strategy_id
                """, (run_date,))
                raw = cur.fetchall()
    except Exception as e:
        print(f'[send_report] closed-positions query failed: {e}')
        return []

    ev_cache: dict = {}
    rows: list[dict] = []
    for r in raw:
        realized = float(r['realized_pnl_pct']) if r['realized_pnl_pct'] is not None else 0.0
        days = int(r['days_held'] or 1)
        key = (str(r['signal_date']), r['ticker'], r['strategy_id'])
        if key not in ev_cache:
            ev_cache[key] = _load_signal_ev(str(r['signal_date']), r['ticker'], r['strategy_id'])
        ev = ev_cache[key]
        if ev is not None:
            kind, sigma_delta = classify(
                realized, days, ev['ev_gbm'], ev['hv21'], sigma_gate=sigma_gate)
        else:
            # No handoff EV (rotated > ~30d, or pre-handoff signal) — count
            # as 'expected' so OUE still sums to total closed.
            kind, sigma_delta = 'expected', None
        rows.append({
            'ticker': r['ticker'],
            'strategy_id': r['strategy_id'],
            'direction': r['direction'],
            'realized_pct': realized,
            'days_held': days,
            'close_reason': r['close_reason'],
            'ticker_notional_usd': float(r['notional_usd']) if r['notional_usd'] is not None else None,
            'oue_kind': kind,
            'oue_sigma_delta': sigma_delta,
        })
    return rows


def _fmt_outcomes_digest(run_date: str,
                          overperf: list[dict],
                          underperf: list[dict],
                          gate: float = 2.0) -> tuple[str, str]:
    """Single-message d-1 outcomes digest. Returns (summary, file_text).

    Summary contains, for each bucket: count, σΔ-magnitude distribution,
    and top-5 rows with ticker/strategy/σΔ — so operators see BOTH
    buckets at a glance and know the spread without opening the file.
    Overperformance is listed first (the positive scenario) so when
    Discord renders a collapsed embed only the over section is cut off
    last — the operator always sees the under section too.

    File attachment contains both full tables (over then under), same
    9-column schema, every row that cleared the σ gate included."""
    if not overperf and not underperf:
        return (f'🟢 **Daily outcomes — d-1 ({run_date})** · '
                f'no positions cleared the {gate:.1f}σ gate either way.', '')

    def _top5(rows: list[dict]) -> str:
        if not rows:
            return '_none_'
        return ', '.join(
            f"`{r.get('ticker')}`/`{(r.get('strategy_id') or '')[:20]}` "
            f"**{(r.get('sigma_delta') or 0):+.2f}σ**"
            for r in rows[:5]
        )

    summary_lines = [
        f'🔭 **Daily outcomes — d-1 ({run_date})** · gate `|σΔ| ≥ {gate:.2f}`',
        '',
        f'🚀 **Overperformance** — **{len(overperf)}** positions · {_sigma_distribution(overperf)}',
        f'   top 5: {_top5(overperf)}',
        '',
        f'🟥 **Underperformance** — **{len(underperf)}** positions · {_sigma_distribution(underperf)}',
        f'   top 5: {_top5(underperf)}',
        '',
        f'_Full tables attached (every row ≥ {gate:.1f}σ)._',
    ]

    # File body: overperformance first (winners on top), then
    # underperformance. Both sections use the same 9-column table.
    file_lines = [
        f'Daily outcomes — d-1 ({run_date})',
        f'Gate: |σΔ| ≥ {gate:.2f}',
        '=' * 60,
        '',
    ]
    file_lines += _fmt_outlier_section(overperf, 'over', gate)
    file_lines += ['', '']
    file_lines += _fmt_outlier_section(underperf, 'under', gate)

    return ('\n'.join(summary_lines), '\n'.join(file_lines))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=str(date.today()))
    ap.add_argument('--dry-run', action='store_true',
                    help='Build the Discord post bodies and print to stdout '
                         'instead of POSTing to webhooks.')
    args = ap.parse_args()
    run_date = args.date
    dry_run  = args.dry_run

    sized = read_handoff(run_date, 'sized') or {}

    # Webhooks from agent_registry (seeded by agent-personas initWebhooks).
    # Posting via webhook URL bypasses bot role permissions — the persistent
    # 403 from the DataBot/TradeDesk bot accounts goes away.
    hooks = _get_webhook_urls('tradedesk')
    wh_signals = hooks.get('trade-signals')
    wh_reports = hooks.get('trade-reports')
    print(f'[send_report] webhook lookup: trade-signals={"ok" if wh_signals else "missing"} '
          f'trade-reports={"ok" if wh_reports else "missing"}')

    # Closed-positions digest (replaces 2026-05-29 the OUE-only digest
    # that showed "0 closed" whenever execution_signals.oue_kind was
    # NULL). Reads the closes straight from signal_pnl — the committed
    # source of truth — grouped by ticker with per-strategy legs, and
    # recomputes OUE live from each signal's handoff EV. Immune to the
    # persisted-oue_kind classifier-ordering bug.
    closed_rows = _load_closed_positions(run_date)
    summary, file_text = _fmt_closed_positions_digest(run_date, closed_rows)

    if dry_run or (not wh_signals and not wh_reports):
        msg = '[send_report] DRY-RUN — printing post bodies to stdout' if dry_run \
              else '[send_report] no webhooks available — printing to stdout only'
        print(msg)
        print('--- #trade-signals body ---')
        print(_fmt_greenlist(run_date, sized))
        print('--- #trade-reports body ---')
        print(summary)
        if file_text:
            print('--- ATTACHMENT (closed_positions.txt) ---')
            print(file_text)
        return 0

    ok1 = _post_webhook(wh_signals, _fmt_greenlist(run_date, sized)) if wh_signals else False

    if wh_reports:
        if file_text:
            ok2 = _post_webhook_with_file(
                wh_reports, summary,
                f'closed_positions_{run_date}.txt', file_text,
            )
        else:
            # Nothing closed — summary is short, no attachment needed.
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
