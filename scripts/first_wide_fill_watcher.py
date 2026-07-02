#!/usr/bin/env python3
"""First WIDE into-close FILL watcher (SP-7 §6 follow-on).

Monday 06-29 16:15 ET produced the first post-§6 WIDE signals (universe 620->5149).
Per the SP-6 Phase A lane those signals are filled into the NEXT session's close:
the Tue 06-30 15:55 ET (19:55 UTC) `eod-into-close-fill` cron sizes (trade) +
submits (alpaca) them. THIS is the first time expanded-universe names can enter the
live book. This watcher waits for that fill to finish and posts a NEW-POSITION
breakdown to #botjohn-log: how many net-new names entered, how many are
§6-attributable (outside the old sp500 clamp), tier split, fills, notional, book.

DETECTION (event-driven — the fix for yesterday's 20s deadline race): poll the
johnbot journal for the cron's own `eod-into-close-fill finished` completion line
(authoritative, always emitted, carries status=/aborted=). Poll a generous 60-min
window. On a confirmed finish -> build + post + self-remove. On deadline -> post
a PARTIAL summary (never a bare red "not detected"), leave units in place.

--dry-run: target today's data as-is, PRINT, do NOT post / clean / require the
journal line. Used to validate before arming.
"""
import os
import re
import sys
import time
import json
import subprocess
from datetime import datetime, timezone

sys.path.insert(0, '/root/openclaw')
sys.path.insert(0, '/root/openclaw/src')
import psycopg2

RUN_DATE        = os.environ.get('OPENCLAW_FWF_RUN_DATE', '2026-06-30')   # fill day (T+1)
FILL_START_UTC  = os.environ.get('OPENCLAW_FWF_FILL_START', f'{RUN_DATE} 19:50:00+00')  # EOD window lower bound
BASELINE_FROM   = os.environ.get('OPENCLAW_FWF_BASE_FROM', '2026-06-15')  # clamped-era traded baseline
BASELINE_TO     = os.environ.get('OPENCLAW_FWF_BASE_TO',   '2026-06-29')
META_SNAPSHOT   = os.environ.get('OPENCLAW_FWF_META_SNAP', '2026-06-29')  # ticker_metadata_snapshots date for tier flags
POLL_INTERVAL_S = 45
POLL_MAX_MIN    = 60
CHANNEL         = os.environ.get('OPENCLAW_FWF_CHANNEL', 'botjohn-log')
ALPACA_BIN      = '/root/go/bin/alpaca'
DRY_RUN         = '--dry-run' in sys.argv


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def log(m):
    print(f"[fwf-watch {_now()}] {m}", flush=True)


def _conn():
    return psycopg2.connect(os.environ['POSTGRES_URI'])


def wait_for_finish():
    """Poll johnbot journal for `eod-into-close-fill finished`. Returns
    (status, aborted) on success, or None on deadline. In --dry-run returns a
    synthetic ('ok','none') immediately (no journal dependency)."""
    if DRY_RUN:
        return ('ok', 'none')
    deadline = time.time() + POLL_MAX_MIN * 60
    pat = re.compile(r'eod-into-close-fill finished:\s*status=(\S+)\s+aborted=(\S+)')
    while True:
        try:
            out = subprocess.run(
                ['journalctl', '--user', '-u', 'johnbot.service',
                 '--since', FILL_START_UTC.replace('+00', ' UTC'), '--no-pager', '-o', 'cat'],
                capture_output=True, text=True, timeout=30).stdout
        except Exception as e:
            log(f"journal read failed (retrying): {e}")
            out = ''
        m = None
        for line in out.splitlines():
            mm = pat.search(line)
            if mm:
                m = mm  # keep last (most recent finish)
        if m:
            return (m.group(1), m.group(2))
        if time.time() >= deadline:
            return None
        log(f"fill-finished line not yet present — sleeping {POLL_INTERVAL_S}s")
        time.sleep(POLL_INTERVAL_S)


def fetch_breakdown(cur):
    """Build the new-position breakdown from alpaca_submissions + tier flags.
    Primary filter = run_date (submitted_at is secondary; submit-errors can have
    NULL submitted_at and must not be dropped)."""
    # All of today's submissions (primary: run_date).
    cur.execute("""
        SELECT ticker, direction, qty, filled_qty, broker_status, alpaca_status,
               notional_usd, filled_avg_price, submitted_at
        FROM alpaca_submissions WHERE run_date = %s
    """, (RUN_DATE,))
    rows = cur.fetchall()
    eod, intraday = [], []
    cutoff = datetime.fromisoformat(FILL_START_UTC)
    for r in rows:
        sat = r[8]
        # NULL submitted_at (submit-error) or >= fill-window -> attribute to EOD fill.
        if sat is None or sat >= cutoff:
            eod.append(r)
        else:
            intraday.append(r)

    def _filled(r):
        fq = r[3]
        if fq is not None and float(fq) != 0:
            return True
        st = (r[4] or r[5] or '').lower()
        return st in ('filled', 'partially_filled')

    def _notional(r):
        if r[6] is not None:
            return abs(float(r[6]))
        if r[3] is not None and r[7] is not None:
            return abs(float(r[3]) * float(r[7]))
        return 0.0

    tickers_eod = sorted({r[0] for r in eod})
    n_orders = len(eod)
    n_filled = sum(1 for r in eod if _filled(r))
    n_buys   = sum(1 for r in eod if (r[1] or '').lower() in ('buy', 'long', '1') or (str(r[1]) == '1'))
    n_sells  = n_orders - n_buys
    gross_notional = sum(_notional(r) for r in eod)

    # Clamped-era traded baseline.
    cur.execute("""SELECT DISTINCT ticker FROM alpaca_submissions
                   WHERE run_date BETWEEN %s AND %s""", (BASELINE_FROM, BASELINE_TO))
    baseline = {r[0] for r in cur.fetchall()}
    net_new = [t for t in tickers_eod if t not in baseline]

    # Tier flags for the net-new names. Use the freshest snapshot <= run_date
    # (falls back to the configured default if none earlier exists).
    tier = {}
    if net_new:
        cur.execute("""SELECT max(snapshot_date) FROM ticker_metadata_snapshots
                       WHERE snapshot_date <= %s""", (RUN_DATE,))
        snap = (cur.fetchone() or [None])[0] or META_SNAPSHOT
        cur.execute("""SELECT symbol, in_sp500, in_r1000, in_r3000, asset_class
                       FROM ticker_metadata_snapshots
                       WHERE snapshot_date = %s AND symbol = ANY(%s)""",
                    (snap, net_new))
        for sym, sp, r1k, r3k, ac in cur.fetchall():
            tier[sym] = {'sp500': sp, 'r1000': r1k, 'r3000': r3k, 'asset_class': ac}

    # §6-attributable = net-new AND not in the old sp500 clamp (the clamp kept
    # in_sp500 equities + non-equity passthrough). Classify by widest tier.
    sp6, buckets = [], {'r1000': [], 'r3000': [], 'wider': [], 'nonequity_or_nometa': []}
    for t in net_new:
        info = tier.get(t)
        if info is None:
            buckets['nonequity_or_nometa'].append(t)  # no equity meta row
            continue
        if info['sp500']:
            continue  # net-new but an S&P 500 name -> not a §6 (clamp) effect
        sp6.append(t)
        if info['r1000']:
            buckets['r1000'].append(t)
        elif info['r3000']:
            buckets['r3000'].append(t)
        elif (info['asset_class'] or 'us_equity') == 'us_equity':
            buckets['wider'].append(t)
        else:
            buckets['nonequity_or_nometa'].append(t)

    return {
        'n_orders': n_orders, 'n_filled': n_filled, 'n_buys': n_buys, 'n_sells': n_sells,
        'n_tickers': len(tickers_eod), 'gross_notional': gross_notional,
        'net_new': net_new, 'sp6': sp6, 'buckets': buckets,
        'n_intraday': len(intraday),
    }


def book_state():
    """Best-effort current book: position count + gross leverage from the alpaca
    CLI (the watcher's systemd env carries ALPACA_* via EnvironmentFile)."""
    out = {'positions': None, 'gross_lev': None}
    try:
        p = subprocess.run([ALPACA_BIN, 'position', 'list'], capture_output=True, text=True, timeout=30)
        data = json.loads(p.stdout)
        positions = data if isinstance(data, list) else data.get('positions') or data.get('data') or []
        if isinstance(positions, list):
            out['positions'] = len(positions)
            gross = sum(abs(float(x.get('market_value') or 0)) for x in positions if isinstance(x, dict))
            acc = subprocess.run([ALPACA_BIN, 'account', 'get'], capture_output=True, text=True, timeout=30)
            adata = json.loads(acc.stdout)
            adata = adata if isinstance(adata, dict) else {}
            equity = float(adata.get('equity') or adata.get('portfolio_value') or 0) or None
            if equity and gross:
                out['gross_lev'] = gross / equity
    except Exception as e:
        log(f"book_state best-effort failed (non-fatal): {e}")
    return out


def build_message(status, aborted, bd, book, partial):
    sp6_n = len(bd['sp6'])
    nn_n  = len(bd['net_new'])
    aborted_clean = aborted in ('none', 'None', None)
    if partial:
        icon = '🟡'
    elif not aborted_clean:
        icon = '🔴'
    elif sp6_n > 0 or nn_n > 0:
        icon = '🟢'
    else:
        icon = '🟡'

    head = ("first into-close FILL of the post-§6 WIDE signals "
            + ("completed" if not partial else "is completing"))
    lines = [
        f"{icon} **SP-7 §6 — FIRST WIDE FILL: new-position breakdown** ({RUN_DATE} into-close)",
        f"the {head} — expanded-universe names can now enter the live book.",
        "",
        f"• **fill lane**: status={status} · aborted={aborted}"
        + ("  ⚠️ *deadline reached, may still be running*" if partial else ""),
        f"• **orders (EOD fill)**: {bd['n_orders']} submitted · {bd['n_filled']} filled · "
        f"{bd['n_buys']} buy / {bd['n_sells']} sell · gross notional ${bd['gross_notional']:,.0f}",
        f"• **net-new tickers** (not traded {BASELINE_FROM}→{BASELINE_TO}): **{nn_n}**",
    ]
    if nn_n:
        b = bd['buckets']
        lines.append(
            f"   └ **§6-attributable** (net-new + outside S&P 500): **{sp6_n}**"
            f"  — r1000:{len(b['r1000'])} · r3000:{len(b['r3000'])} · wider:{len(b['wider'])}"
            f" · non-eq/no-meta:{len(b['nonequity_or_nometa'])}"
        )
        sample = bd['sp6'][:12] or bd['net_new'][:12]
        extra = max(0, (sp6_n or nn_n) - len(sample))
        lines.append("   └ sample: " + ", ".join(sample) + (f"  (+{extra} more)" if extra else ""))
    if book.get('positions') is not None:
        lev = f" · gross leverage {book['gross_lev']:.2f}x" if book.get('gross_lev') else ""
        lines.append(f"• **book now**: {book['positions']} positions{lev}")
    if bd['n_intraday']:
        lines.append(f"• (note: {bd['n_intraday']} intraday orders earlier today, excluded from the EOD count)")
    lines.append("")
    lines.append("clamped→wide breadth phases in over several sessions — the sizer nets "
                 "deltas and is bounded by the 25%/cycle turnover cap + DTBP + asset-corr cap, "
                 "so new names compete for BP rather than flooding in at once.")
    return "\n".join(lines)


def post(msg):
    if DRY_RUN:
        log("DRY-RUN — would post to #%s:\n%s" % (CHANNEL, msg))
        return True
    try:
        from src.execution.pipeline_orchestrator import post_channel
        return post_channel(CHANNEL, msg)
    except Exception as e:
        log(f"post failed: {e}")
        return False


def self_remove():
    # No shell, fixed argv + os.remove (no user input anywhere here).
    if DRY_RUN:
        return
    try:
        unit = os.path.expanduser('~/.config/systemd/user')
        subprocess.run(['systemctl', '--user', 'disable', 'first-wide-fill-watch.timer'],
                       capture_output=True, timeout=10)
        for f in ('first-wide-fill-watch.timer', 'first-wide-fill-watch.service'):
            try:
                os.remove(os.path.join(unit, f))
            except FileNotFoundError:
                pass
        subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True, timeout=10)
        log("self-removed timer+service units")
    except Exception as e:
        log(f"self-remove failed (non-fatal): {e}")


def main():
    log(f"start — run_date={RUN_DATE} dry_run={DRY_RUN} poll<= {POLL_MAX_MIN}min")
    fin = wait_for_finish()
    partial = fin is None
    status, aborted = fin if fin else ('unknown', 'unknown')
    if partial:
        log("DEADLINE — no fill-finished line; posting PARTIAL summary (units left in place)")
    else:
        log(f"fill finished: status={status} aborted={aborted}")
        time.sleep(8)  # let reconcile settle the broker_status/filled_qty writes

    conn = _conn()
    try:
        with conn.cursor() as cur:
            bd = fetch_breakdown(cur)
    finally:
        conn.close()
    book = book_state()
    msg = build_message(status, aborted, bd, book, partial)
    posted = post(msg)
    log(f"posted={posted} partial={partial} net_new={len(bd['net_new'])} sp6={len(bd['sp6'])}")
    if posted and not partial and not DRY_RUN:
        self_remove()
    return 0  # never exit non-zero into a failed unit; partial leaves units for re-run


if __name__ == '__main__':
    sys.exit(main())
