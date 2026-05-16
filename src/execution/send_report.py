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


def _fmt_oue_today_digest(run_date: str) -> tuple[str, str]:
    """OUE summary for trades that closed in the LAST DAY (since-prev cycle).

    Each closed trade is classified once by oue_classifier into one of
    {over, under, expected} based on its realized return vs the GBM
    expectation captured at signal time. The invariant is:
        O + U + E = total closed today
    (vs the old over/under-only metric where a trade could appear in
    multiple cycles or never appear at all if it stayed within band).

    Returns (summary, file_text). file_text is empty when there are no
    closes today — keeps the post compact.
    """
    import os
    uri = os.environ.get('POSTGRES_URI', '')
    if not uri:
        return (f'🟢 **OUE — Today’s closures ({run_date})** · '
                f'no DB configured (POSTGRES_URI missing).', '')
    try:
        with psycopg2.connect(uri) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT es.id, es.strategy_id, es.ticker, es.direction,
                           es.oue_kind, es.oue_sigma_delta,
                           sp.realized_pnl_pct, sp.days_held, sp.close_reason
                      FROM execution_signals es
                      JOIN LATERAL (
                          SELECT realized_pnl_pct, days_held, close_reason, pnl_date
                            FROM signal_pnl
                           WHERE signal_id = es.id
                             AND status = 'closed'
                             AND realized_pnl_pct IS NOT NULL
                             AND pnl_date::date = %s
                           ORDER BY pnl_date DESC
                           LIMIT 1
                      ) sp ON TRUE
                     WHERE es.status = 'closed'
                       AND es.oue_kind IS NOT NULL
                     ORDER BY es.oue_kind, es.oue_sigma_delta DESC NULLS LAST
                """, (run_date,))
                rows = cur.fetchall()
    except Exception as e:
        return (f'🟢 **OUE — Today’s closures ({run_date})** · DB query failed: {e}', '')

    if not rows:
        return (f'🟢 **OUE — Today’s closures ({run_date})** · '
                f'no trades closed in this cycle.', '')

    by_kind = {'over': [], 'under': [], 'expected': []}
    for r in rows:
        kind = r['oue_kind']
        if kind in by_kind:
            by_kind[kind].append(r)
    n_o, n_u, n_e = len(by_kind['over']), len(by_kind['under']), len(by_kind['expected'])
    total = n_o + n_u + n_e

    def _fmt_top(rows: list[dict], n: int = 5) -> str:
        if not rows:
            return '_none_'
        return ', '.join(
            f"`{r.get('ticker')}`/`{(r.get('strategy_id') or '')[:18]}` "
            f"**{float(r.get('oue_sigma_delta') or 0):+.2f}σ** "
            f"({float(r.get('realized_pnl_pct') or 0) * 100:+.2f}%)"
            for r in rows[:n]
        )

    summary_lines = [
        f'🎯 **OUE — Today’s closures ({run_date})** · **{total}** trade(s) closed',
        '',
        f'🚀 **Over**     — **{n_o}** ({100 * n_o / total:.0f}%) — '
        f'realized > expectation by ≥1σ',
        f'🟥 **Under**    — **{n_u}** ({100 * n_u / total:.0f}%) — '
        f'realized < expectation by ≥1σ',
        f'🟢 **Expected** — **{n_e}** ({100 * n_e / total:.0f}%) — '
        f'within ±1σ of expectation',
        '',
        f'Top over (top 5): {_fmt_top(by_kind["over"])}',
        f'Top under (top 5): {_fmt_top(by_kind["under"])}',
        '',
        f'_O + U + E = {n_o} + {n_u} + {n_e} = {total} (invariant)._',
    ]

    file_lines = [
        f'OUE — Today’s closures ({run_date})',
        '=' * 60,
        '',
        f'Total closed:  {total}',
        f'Over:          {n_o}',
        f'Under:         {n_u}',
        f'Expected:      {n_e}',
        '',
    ]
    header = f'{"ticker":<7}  {"strategy":<28}  {"dir":<5}  {"kind":<8}  {"σΔ":>6}  {"realized":>9}  {"days":>4}  reason'
    sep = '-' * len(header)
    for kind_name in ('over', 'under', 'expected'):
        kind_rows = by_kind[kind_name]
        if not kind_rows:
            continue
        file_lines += [
            '',
            f'── {kind_name.upper()} ({len(kind_rows)}) ──',
            header, sep,
        ]
        for r in kind_rows:
            file_lines.append(
                f'{r.get("ticker") or "":<7}  '
                f'{(r.get("strategy_id") or "")[:28]:<28}  '
                f'{(r.get("direction") or "")[:5]:<5}  '
                f'{(r.get("oue_kind") or ""):<8}  '
                f'{float(r.get("oue_sigma_delta") or 0):>+6.2f}  '
                f'{float(r.get("realized_pnl_pct") or 0) * 100:>+8.2f}%  '
                f'{int(r.get("days_held") or 0):>4}  '
                f'{r.get("close_reason") or ""}'
            )
    return ('\n'.join(summary_lines), '\n'.join(file_lines))


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

    # OUE — Today's Closures (replaces 2026-05-16 the legacy over/under
    # digest built from the unrealized-fallback handoff). Pulls
    # execution_signals.oue_kind for trades that closed in this cycle,
    # showing O / U / E counts and top realized moves. Invariant
    # O + U + E = total closed today is enforced by the classifier
    # marking every close exactly once.
    summary, file_text = _fmt_oue_today_digest(run_date)

    if dry_run or (not wh_signals and not wh_reports):
        msg = '[send_report] DRY-RUN — printing post bodies to stdout' if dry_run \
              else '[send_report] no webhooks available — printing to stdout only'
        print(msg)
        print('--- #trade-signals body ---')
        print(_fmt_greenlist(run_date, sized))
        print('--- #trade-reports body ---')
        print(summary)
        if file_text:
            print('--- ATTACHMENT (outcomes_d-1.txt) ---')
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
