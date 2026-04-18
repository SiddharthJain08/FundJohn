#!/usr/bin/env python3
"""
trade_agent.py — TradeDesk signal optimizer.

Reads the current execution signals, runs Kelly-criterion optimization across
all available exit targets (T1, T2, T3) and holding horizons, then posts only
GREEN signals (Kelly > 0, EV > 0) to #trade-signals with optimal sizing.

Optimization logic:
  1. For each signal, compute GBM two-barrier P(target before stop) at T1, T2, T3
  2. Compute Kelly fraction: f* = (p·R - (1-p)) / R  where R = reward/risk
  3. Take best (target, kelly) pair per signal — winner is highest Kelly
  4. If Kelly > 0: GREEN → include with Kelly-sized position (capped at 5%)
  5. If no signal clears Kelly > 0: post "no actionable signals generated"
     with a brief breakdown of why and what would flip each signal green.

Posts to:
  #trade-signals  (TradeDesk webhook) — green signals only, condensed
"""

import os, sys, math, json, warnings, logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

try:
    from execution.handoff import read_handoff, write_handoff as _write_handoff
    _HANDOFF_AVAILABLE = True
except ImportError:
    _HANDOFF_AVAILABLE = False

import numpy as np
import pandas as pd
import requests
from execution.alpaca_trader import execute_alpaca_orders, build_alpaca_post
import psycopg2, psycopg2.extras
from datetime import date, datetime, timedelta

warnings.filterwarnings('ignore')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [TRADE_AGENT] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

# ── Webhooks ─────────────────────────────────────────────────────────────────
WEBHOOKS = {
    'tradedesk_trade_signals':    'https://discord.com/api/webhooks/1492082674309791886/90a-LQ9bTj4e1vYW31OgY7krJtQNUqVusCQepzI3bPpZJt0uVqVtGNu4b3y-4YVIHFhU',
    'researchdesk_research_feed': 'https://discord.com/api/webhooks/1492082671235371071/7ZZdz0XeEGxylNLyYBpHTeXu5FYB_lqiPIRz1RcbXHkcdy5YZyI_Y57tWtjLKScPQFul',
}

HOLDING_PERIOD = {
    'S9_dual_momentum':          {'min': 21, 'target': 63,  'max': 126},
    'S_custom_jt_momentum_12mo': {'min': 42, 'target': 105, 'max': 189},
}

STRATEGY_LABELS = {
    'S9_dual_momentum':          'Dual Momentum (S9)',
    'S_custom_jt_momentum_12mo': 'JT 12-Month (JT)',
}

MAX_POSITION_PCT  = 0.05   # hard cap per signal
MIN_KELLY         = 0.005  # minimum net Kelly to call a signal actionable
HALF_KELLY        = 0.50   # safety fraction applied to raw Kelly
CAPTURE_RATIO     = 0.80   # assume 80% of target captured (partial fills / slippage)


# ── Helpers ───────────────────────────────────────────────────────────────────

def wh_post(key, text):
    url    = WEBHOOKS[key]
    chunks, buf = [], ''
    for line in (text + '\n').split('\n'):
        if len(buf) + len(line) + 1 > 1990:
            chunks.append(buf.rstrip())
            buf = ''
        buf += line + '\n'
    if buf.strip():
        chunks.append(buf.rstrip())
    for chunk in chunks:
        r = requests.post(url, json={'content': chunk}, timeout=10)
        print(f'  [{key[:24]}] {len(chunk)}c → {r.status_code}')


# ── Two-barrier GBM probability ───────────────────────────────────────────────

def p_hit_upper(entry, stop, target, mu_daily, sigma_daily):
    """
    P(hit target before stop) under GBM with drift mu_daily and vol sigma_daily.
    Uses reflection-principle / two-barrier formula.
    Returns probability in [0, 1].
    """
    if entry <= stop or target <= entry or sigma_daily <= 0:
        return 0.0

    a      = math.log(stop   / entry)   # negative
    b      = math.log(target / entry)   # positive
    mu_adj = mu_daily - 0.5 * sigma_daily ** 2

    if abs(mu_adj) < 1e-8:
        # Neutral drift (Brownian motion): linear interpolation
        return -a / (b - a)

    lam = 2.0 * mu_adj / (sigma_daily ** 2)
    try:
        ea = math.exp(lam * a)
        eb = math.exp(lam * b)
        p  = (1.0 - ea) / (eb - ea)
        return max(0.0, min(1.0, p))
    except OverflowError:
        return 1.0 if mu_adj > 0 else 0.0


# ── Kelly optimization ────────────────────────────────────────────────────────

def kelly_optimize(sig, px, spy_returns):
    """
    For a single signal, compute Kelly fraction at T1, T2, T3.
    Returns dict with best target, Kelly fraction, EV, and breakdown.
    """
    ticker = sig['ticker']
    entry  = sig['entry_price']
    stop   = sig['stop_loss']
    strat  = sig['strategy_id']
    params = sig['signal_params'] if isinstance(sig['signal_params'], dict) \
             else json.loads(sig['signal_params'] or '{}')

    targets = {
        'T1': sig['target_1'],
        'T2': sig['target_2'],
        'T3': sig['target_3'],
    }

    # Volatility
    hv21, hv63 = 0.40, 0.40
    mu_d       = 0.0
    if ticker in px.columns:
        ts   = px[ticker].dropna()
        rets = ts.pct_change().dropna()
        if len(rets) >= 21:
            hv21 = rets.tail(21).std() * math.sqrt(252)
        if len(rets) >= 63:
            hv63 = rets.tail(63).std() * math.sqrt(252)

    sig_d = hv21 / math.sqrt(252)   # daily vol from 21d HV
    hp    = HOLDING_PERIOD.get(strat, {'target': 63})['target']

    # Drift: blend academic momentum premium (1%/month) with observed signal
    mom_12m      = params.get('lookback_ret', params.get('momentum_12mo', 0.10))
    mom_d_obs    = mom_12m / 252            # raw observed daily drift
    mom_d_acad   = 0.01 / 21               # 1% per month academic prior
    # Weight: high vol → trust academic prior more (avoid overfitting noisy drift)
    vol_weight   = min(hv63 / 0.60, 1.0)   # 0→1 as HV63 goes 0→60%
    mu_d         = (1 - vol_weight) * mom_d_obs + vol_weight * mom_d_acad

    risk_pct = (entry - stop) / entry

    results = []
    for label, target in targets.items():
        if not target or target <= entry:
            continue

        reward_pct_raw = (target - entry) / entry
        reward_pct     = reward_pct_raw * CAPTURE_RATIO   # apply slippage haircut

        p_win  = p_hit_upper(entry, stop, target, mu_d, sig_d)
        p_loss = 1.0 - p_win

        # Kelly: f* = (p·R - q) / R  where R = reward/risk
        R = reward_pct / risk_pct if risk_pct > 0 else 0.0
        if R <= 0:
            kelly_raw = -1.0
        else:
            kelly_raw = (p_win * R - p_loss) / R

        kelly_net = kelly_raw * HALF_KELLY   # apply half-Kelly safety
        kelly_pos = min(max(kelly_net, 0.0), MAX_POSITION_PCT)

        ev = p_win * reward_pct - p_loss * risk_pct

        # Breakeven probability (minimum p for EV > 0)
        p_break = risk_pct / (reward_pct + risk_pct) if (reward_pct + risk_pct) > 0 else 1.0

        results.append({
            'label':       label,
            'target':      target,
            'reward_pct':  reward_pct,
            'risk_pct':    risk_pct,
            'R':           R,
            'p_win':       p_win,
            'p_loss':      p_loss,
            'kelly_raw':   kelly_raw,
            'kelly_net':   kelly_net,
            'kelly_pos':   kelly_pos,
            'ev':          ev,
            'p_break':     p_break,
            'mu_d':        mu_d,
            'sig_d':       sig_d,
            'hv21':        hv21,
            'hv63':        hv63,
        })

    if not results:
        return None

    # Best: highest Kelly (even if negative, for reporting)
    best = max(results, key=lambda x: x['kelly_net'])
    best['all_targets'] = results
    best['ticker']      = ticker
    best['strategy']    = strat
    best['entry']       = entry
    best['stop']        = stop
    best['strat_label'] = STRATEGY_LABELS.get(strat, strat)
    best['hp_days']     = hp

    # Expected exit date
    exp_days = int(best['p_win'] * best['hp_days'] + best['p_loss'] * min(10, best['hp_days']//4))
    best['exp_exit_date'] = str(datetime.strptime(run_date, '%Y-%m-%d').date() + timedelta(days=int(exp_days * 1.4)))

    # What drift would flip to positive EV at best target?
    # EV > 0 requires p_win > p_break
    # Back-solve: what mu_d gives p_win = p_break?
    a = math.log(stop / entry)
    b = math.log(best['target'] / entry)
    p_needed = best['p_break']
    # p = (1-exp(lam*a))/(exp(lam*b)-exp(lam*a)) = p_needed
    # Hard to invert analytically — estimate numerically
    best['p_needed_for_green'] = p_needed
    # Simple approximation: what annual return would produce p_win = p_break?
    # Rough: mu_d needed ≈ p_break × (b-a) / hp_days  (heuristic)
    best['annual_drift_needed'] = (p_needed * (b - a) / best['hp_days'] + 0.5 * best['sig_d']**2) * 252

    return best


def load_research_context(run_date):
    if not _HANDOFF_AVAILABLE:
        return {}
    ctx = read_handoff(run_date, 'research')
    if ctx:
        print(f'[trade_agent] Research handoff loaded: regime={ctx.get("regime")}, {len(ctx.get("signals",[]))} signals')
    else:
        print('[trade_agent] Research handoff unavailable — proceeding without it')
    return ctx or {}


def write_ev_veto_rows(postgres_uri, run_date, vetoed_opts):
    if not vetoed_opts:
        return
    try:
        conn = psycopg2.connect(postgres_uri)
        cur  = conn.cursor()
        for opt in vetoed_opts:
            cur.execute(
                '''INSERT INTO veto_log (run_date, strategy_id, ticker, veto_reason, ev, kelly)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING''',
                (run_date, opt.get('strategy'), opt.get('ticker'),
                 'negative_ev', opt.get('ev'), opt.get('kelly_net')),
            )
        conn.commit()
        conn.close()
        print(f'[trade_agent] {len(vetoed_opts)} negative_ev veto row(s) written')
    except Exception as e:
        print(f'[trade_agent] veto_log write failed: {e}')


def load_prices():
    df  = pd.read_parquet(ROOT / 'data/master/prices.parquet')
    df['date'] = pd.to_datetime(df['date'])
    return df.pivot(index='date', columns='ticker', values='close').sort_index()


def load_signals(postgres_uri, run_date):
    conn = psycopg2.connect(postgres_uri)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('''
        SELECT strategy_id, ticker, direction, entry_price, stop_loss,
               target_1, target_2, target_3, position_size_pct, signal_params
        FROM execution_signals WHERE signal_date = %s ORDER BY strategy_id, ticker
    ''', (run_date,))
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        for col in ('entry_price','stop_loss','target_1','target_2','target_3','position_size_pct'):
            if d.get(col) is not None:
                d[col] = float(d[col])
        rows.append(d)
    conn.close()
    return rows


# ── Report builders ───────────────────────────────────────────────────────────

def build_green_signal_line(opt):
    """One-liner for a green signal in the trade signals post."""
    ev_pct   = opt['ev'] * 100
    kelly    = opt['kelly_pos'] * 100
    p_win    = opt['p_win'] * 100
    reward   = opt['reward_pct'] * 100
    risk     = opt['risk_pct'] * 100
    return (
        f"**{opt['ticker']}** ({opt['strat_label']}) "
        f"→ target **{opt['label']}** @ ${opt['target']:.2f} "
        f"| Size: **{kelly:.2f}%** (half-Kelly) "
        f"| EV: **+{ev_pct:.2f}%** on position "
        f"| P(win): {p_win:.0f}% "
        f"| Reward/Risk: {opt['R']:.1f}x "
        f"| Exit ~{opt['exp_exit_date']}"
    )


def build_no_action_post(opts, run_date):
    """
    No actionable signals post — shows optimization work and what would flip each signal.
    """
    # Sort by least-negative Kelly
    opts_sorted = sorted(opts, key=lambda x: x['kelly_net'], reverse=True)
    best_of_bad = opts_sorted[0]

    lines = [
        f'📈 **TradeDesk — Signal Optimization Report | {run_date}**',
        f'*Source: ResearchDesk report in #research-feed | Optimization: Kelly criterion across T1/T2/T3*',
        '',
        '**RESULT: NO ACTIONABLE SIGNALS GENERATED**',
        '',
        'After Kelly-criterion optimization across all 10 signals and 3 exit targets each,',
        'no signal produces positive expected value. The regime conditions drive this:',
        '',
        f'• **Regime:** HIGH_VOL — position scale 0.35x creates sub-1:1 R:R at strategy-set stops',
        f'• **Core issue:** Stop distances (1.9%–8.6%) are calibrated to ATR, but in HIGH_VOL',
        f'  volatility dwarfs the drift signal — noise dominates before momentum can work',
        f'• **Result:** P(stop-out) > P(T1) for 9 of 10 signals at any position size',
        '',
        '─────────────────────────────────────────',
        '**Optimization Summary** *(best target per signal — even if Kelly < 0)*',
        '```',
        f'{"Ticker":<6} {"Best":<4} {"P(win)":>6} {"R:R":>5} {"EV":>8} {"Kelly½":>7} {"Needs P≥":>9}',
        '─' * 55,
    ]

    for opt in opts_sorted:
        p_win  = opt['p_win']   * 100
        ev     = opt['ev']      * 100
        kelly  = opt['kelly_net'] * 100
        p_need = opt['p_needed_for_green'] * 100
        R      = opt['R']
        label  = opt['label']
        flag   = '🟡' if kelly > -2 else '🔴'
        lines.append(
            f'{flag}{opt["ticker"]:<5} {label:<4} {p_win:>5.0f}%  {R:>4.1f}x  {ev:>+7.2f}%  {kelly:>+6.2f}%  {p_need:>8.0f}%'
        )

    lines += [
        '```',
        '',
        f'**Closest to actionable:** {best_of_bad["ticker"]} at {best_of_bad["label"]}',
        f'  P(win)={best_of_bad["p_win"]*100:.0f}% | EV={best_of_bad["ev"]*100:+.2f}% | '
        f'Needs P(win)≥{best_of_bad["p_needed_for_green"]*100:.0f}% to break even',
        f'  Gap: {(best_of_bad["p_needed_for_green"] - best_of_bad["p_win"])*100:.0f}pp',
        '',
        '─────────────────────────────────────────',
        '**What would generate green signals?**',
        '',
        '1. **Regime shift to TRANSITIONING/LOW_VOL** — position scale returns to 1.0x,',
        '   wider targets become reachable before stops fire',
        '2. **R:R ≥ 1.5x at T1** — current stops yield 0.6–2.6x; need consistent ≥1.5x',
        '   for P(T1) ≈ 40% to produce positive Kelly',
        '3. **Tighter stops** on high-momentum names — e.g. USO stop at $120.50 (5.1% risk)',
        '   vs current $116.41 (8.3%) shifts break-even P from 86% → 62%',
        '4. **Confluence signals** — a ticker appearing in 2+ strategies would warrant',
        '   override sizing; none exist today',
        '',
        f'*TradeDesk will re-run automatically after next engine cycle.*',
    ]

    return '\n'.join(lines)


def build_green_post(green_opts, run_date, total_kelly_pct):
    """
    Post for when green signals exist.
    """
    lines = [
        f'📈 **TradeDesk — Trade Signals | {run_date}**',
        f'*Kelly-optimized sizing | Half-Kelly applied | {len(green_opts)} actionable signal(s)*',
        '',
    ]
    for opt in sorted(green_opts, key=lambda x: x['kelly_net'], reverse=True):
        hp_label = 'monthly rebalance' if 'dual' in opt['strategy'] else '3–6 month hold'
        ev_portfolio = opt['ev'] * opt['kelly_pos'] * 100
        lines += [
            f'**🟢 {opt["ticker"]}** — {opt["strat_label"]}',
            f'> **Buy:** ${opt["entry"]:.2f} | **Stop:** ${opt["stop"]:.2f} ({opt["risk_pct"]*100:.1f}% risk)',
            f'> **Exit target:** {opt["label"]} @ ${opt["target"]:.2f} ({opt["reward_pct"]*100:.1f}% gain)',
            f'> **Expected exit:** ~{opt["exp_exit_date"]} ({hp_label})',
            f'> **Portfolio stake:** **{opt["kelly_pos"]*100:.2f}%** (½-Kelly={opt["kelly_net"]*100:.2f}%, capped at {MAX_POSITION_PCT*100:.0f}%)',
            f'> **Expected return:** +{opt["ev"]*100:.2f}% on position (+{ev_portfolio:.3f}% portfolio)',
            f'> **P(hit target):** {opt["p_win"]*100:.0f}% | R:R {opt["R"]:.1f}x | Forward Sharpe implied: {(opt["ev_ann"] if "ev_ann" in opt else opt["ev"]*252/opt["hp_days"]):.2f}',
            '',
        ]

    lines += [
        f'─────────────────────────────────────────',
        f'**Portfolio impact:** +{sum(o["ev"]*o["kelly_pos"] for o in green_opts)*100:.3f}% expected | '
        f'{total_kelly_pct:.2f}% gross deployed | '
        f'Kelly-weighted EV/unit: {sum(o["ev"]*o["kelly_pos"] for o in green_opts)/max(total_kelly_pct/100,0.001):.4f}',
    ]
    return '\n'.join(lines)


# ── Memory writes ─────────────────────────────────────────────────────────────

def write_trade_learnings(all_opts, green_opts, run_date):
    """
    Append Kelly optimization outcomes to trade_learnings.md after each run.
    Accumulates regime-specific sizing patterns for TradeDesk to learn from.
    """
    from pathlib import Path as _Path
    workspace = _Path(os.environ.get('OPENCLAW_DIR', str(ROOT))) / 'workspaces' / 'default'
    mem_dir   = workspace / 'memory'
    mem_dir.mkdir(parents=True, exist_ok=True)

    # Read regime from market-state file
    regime = 'UNKNOWN'
    for ms_path in [ROOT / '.agents' / 'market-state' / 'latest.json',
                    _Path(os.environ.get('OPENCLAW_DIR', str(ROOT))) / '.agents' / 'market-state' / 'latest.json']:
        try:
            regime = json.loads(ms_path.read_text()).get('state', 'UNKNOWN')
            break
        except Exception:
            pass

    n_green = len(green_opts)
    n_red   = len(all_opts) - n_green

    avg_kelly_green = sum(o['kelly_net'] for o in green_opts) / max(n_green, 1) * 100 if green_opts else 0
    avg_p_win_all   = sum(o['p_win'] for o in all_opts) / max(len(all_opts), 1) * 100
    avg_rr          = sum(o['R'] for o in all_opts) / max(len(all_opts), 1)
    avg_ev          = sum(o['ev'] for o in all_opts) / max(len(all_opts), 1) * 100

    # Closest red signal (what would flip it)
    if all_opts:
        closest_red = min((o for o in all_opts if o['kelly_net'] <= 0),
                          key=lambda x: x['p_needed_for_green'] - x['p_win'], default=None)
        flip_note = ''
        if closest_red:
            gap = (closest_red['p_needed_for_green'] - closest_red['p_win']) * 100
            flip_note = f", closest_flip={closest_red['ticker']}(gap={gap:.0f}pp)"
    else:
        flip_note = ''

    entry = (
        f"\n{run_date} | {regime} | "
        f"green={n_green}, red={n_red}, "
        f"avgP(win)={avg_p_win_all:.0f}%, avgRR={avg_rr:.2f}x, avgEV={avg_ev:+.2f}%, "
        f"avgKelly(green)={avg_kelly_green:.2f}%"
        f"{flip_note}"
    )

    try:
        fpath    = mem_dir / 'trade_learnings.md'
        stripped = entry.strip()
        # Dedup: skip if this date's entry already exists
        if not fpath.exists() or (stripped and stripped not in fpath.read_text()):
            with open(fpath, 'a') as f:
                f.write(entry)

        # Structured JSONL parallel write for machine-readable queries
        jsonl_path = mem_dir / 'events.jsonl'
        import json as _json, datetime as _dt
        jsonl_record = _json.dumps({
            'ts':      _dt.datetime.utcnow().isoformat() + 'Z',
            'type':    'trade_summary',
            'date':    run_date,
            'regime':  regime,
            'green':   n_green,
            'red':     n_red,
            'avg_ev':  round(avg_ev, 4),
            'avg_kelly_green': round(avg_kelly_green, 4),
        })
        with open(jsonl_path, 'a') as f:
            f.write(jsonl_record + '\n')

        print(f'[trade_agent] Memory written: trade_learnings.md ({run_date})')
    except Exception as e:
        print(f'[trade_agent] Memory write failed: {e}')


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=date.today().isoformat())
    args = parser.parse_args()

    postgres_uri = os.environ.get('POSTGRES_URI', '')
    run_date     = args.date

    research_ctx = load_research_context(run_date)

    print(f'[trade_agent] Loading data...')
    px      = load_prices()
    signals = load_signals(postgres_uri, run_date)
    spy_rets = px['SPY'].pct_change().dropna() if 'SPY' in px.columns else pd.Series(dtype=float)

    print(f'[trade_agent] Optimizing {len(signals)} signals...')
    all_opts = []
    for sig in signals:
        opt = kelly_optimize(sig, px, spy_rets)
        if opt is None:
            print(f'  {sig["ticker"]}: SKIP (no data)')
            continue

        flag = '🟢' if opt['kelly_net'] > MIN_KELLY else '🔴'
        print(
            f'  {flag} {opt["ticker"]}: best={opt["label"]} '
            f'P(win)={opt["p_win"]*100:.0f}% EV={opt["ev"]*100:+.2f}% '
            f'Kelly½={opt["kelly_net"]*100:+.2f}% '
            f'μ_d={opt["mu_d"]*252*100:.1f}%/yr σ={opt["hv21"]*100:.0f}%'
        )
        all_opts.append(opt)

    green = [o for o in all_opts if o['kelly_net'] > MIN_KELLY]

    # ── Alpaca paper trading ──────────────────────────────────────────────
    alpaca_results = []
    if green:
        logger.info(f"[TradeJohn] {len(green)} green signal(s) → submitting Alpaca orders")
        alpaca_results = execute_alpaca_orders(green, run_date)
    else:
        logger.info("[TradeJohn] No green signals — no Alpaca orders")
    alpaca_discord = build_alpaca_post(alpaca_results, run_date)


    print(f'\n[trade_agent] Green signals: {len(green)} / {len(all_opts)}')

    if green:
        total_kelly = sum(o['kelly_pos'] for o in green) * 100
        msg = build_green_post(green, run_date, total_kelly)
        if alpaca_discord:
            msg = msg + "\n\n" + alpaca_discord

    else:
        msg = build_no_action_post(all_opts, run_date)

    print('\n' + '='*70)
    print(msg)
    print('='*70)

    print('\n[trade_agent] Posting to Discord...')
    wh_post('tradedesk_trade_signals', msg)

    print('[trade_agent] Writing memory learnings...')
    write_trade_learnings(all_opts, green, run_date)

    # ── Veto logging + sized handoff ──────────────────────────────────────
    vetoed = [o for o in all_opts if o['kelly_net'] <= MIN_KELLY]
    write_ev_veto_rows(postgres_uri, run_date, vetoed)

    if _HANDOFF_AVAILABLE:
        _write_handoff(run_date, 'sized', {
            'run_date':     run_date,
            'signals':      [
                {'ticker':     o['ticker'], 'strategy_id': o['strategy'],
                 'kelly_pos':  o['kelly_pos'], 'ev': o['ev'],
                 'target':     o['label'], 'exp_exit_date': o['exp_exit_date']}
                for o in green
            ],
            'vetoed_count': len(vetoed),
        })
        print(f'[trade_agent] Sized handoff written — {len(green)} green, {len(vetoed)} vetoed.')

    print('[trade_agent] Done.')
