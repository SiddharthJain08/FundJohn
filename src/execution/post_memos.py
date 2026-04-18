#!/usr/bin/env python3
"""
post_memos.py — Run execution engine then post strategy memo + research synthesis.

Usage:
    python3 src/execution/post_memos.py [--date YYYY-MM-DD]

Flow:
  1. Run engine.py → writes signals for today
  2. Query all signals for the target date
  3. DataBot webhook → #strategy-memos  (full per-signal table)
  4. ResearchDesk webhook → #research-feed  (synthesis: top picks, risk flags, sizing)
"""

import os, sys, json, subprocess, requests, psycopg2, psycopg2.extras
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

try:
    from execution.lint_memo import lint_memo, write_veto_rows
    from execution.handoff import write_handoff
    _HANDOFF_AVAILABLE = True
except ImportError:
    _HANDOFF_AVAILABLE = False

# ── Webhook URLs (set at startup by bot.js / agent-personas.js) ──────────────
WEBHOOKS = {
    'databot_strategy_memos':      'https://discord.com/api/webhooks/1492623936247300186/BFUwcy91xaIzq_GwP_YvON9-N9HhSilx-wDQ6MhISRYoSx9LrNYyXsDQeaSzxfwimEBi',
    'researchdesk_strategy_memos': 'https://discord.com/api/webhooks/1492623938092535958/0bsB7NH54SPd1Z71eD1ieg3Nsm4uYE0OqNULjzxY5Tb1QxLzUK2rXsnpTPTV9-u64b5J',
    'researchdesk_research_feed':  'https://discord.com/api/webhooks/1492082671235371071/7ZZdz0XeEGxylNLyYBpHTeXu5FYB_lqiPIRz1RcbXHkcdy5YZyI_Y57tWtjLKScPQFul',
    'tradedesk_trade_signals':     'https://discord.com/api/webhooks/1492082674309791886/90a-LQ9bTj4e1vYW31OgY7krJtQNUqVusCQepzI3bPpZJt0uVqVtGNu4b3y-4YVIHFhU',
}

# ── Strategy labels: built dynamically from registry ─────────
def _build_strategy_labels() -> dict:
    """Load all registered strategy IDs and build human-readable labels."""
    try:
        import sys as _sys
        _sys.path.insert(0, str(ROOT / 'src'))
        from strategies.registry import list_all_strategy_ids
        labels = {}
        for sid in list_all_strategy_ids():
            # Convert 'S_HV16_gex_regime' -> 'S-HV16 Gex Regime'
            parts = sid.replace('S_HV', 'S-HV').replace('_', ' ').split()
            label = ' '.join(p.capitalize() if not p.startswith('S-') else p for p in parts)
            labels[sid] = label
        return labels
    except Exception as e:
        print(f'[post_memos] Could not load registry labels: {e}')
        return {}

STRATEGY_LABELS = _build_strategy_labels()


# ── Helpers ──────────────────────────────────────────────────────────────────

def wh_post(webhook_key, text):
    """Post text via webhook, chunking at 1990 chars."""
    url = WEBHOOKS[webhook_key]
    chunks, buf = [], ''
    for line in text.split('\n'):
        if len(buf) + len(line) + 1 > 1990:
            chunks.append(buf.rstrip())
            buf = line + '\n'
        else:
            buf += line + '\n'
    if buf.strip():
        chunks.append(buf.rstrip())

    for chunk in chunks:
        r = requests.post(url, json={'content': chunk}, timeout=10)
        if not r.ok:
            print(f'[post_memos] Webhook {webhook_key} failed: {r.status_code} {r.text[:100]}')
        else:
            print(f'[post_memos] → {webhook_key} ({len(chunk)} chars) {r.status_code}')


# ── Engine run ────────────────────────────────────────────────────────────────

def run_engine():
    env = {**os.environ, 'PYTHONPATH': str(ROOT) + ':' + str(ROOT / 'src')}
    result = subprocess.run(
        ['python3', '-m', 'src.execution.engine'],
        cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=120,
    )
    print(result.stderr.strip())
    # Last line is JSON metrics
    for line in reversed(result.stdout.strip().split('\n')):
        try:
            return json.loads(line)
        except Exception:
            continue
    return {}


# ── Query signals ─────────────────────────────────────────────────────────────

def query_signals(postgres_uri, workspace_id, run_date):
    conn = psycopg2.connect(postgres_uri)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('''
        SELECT strategy_id, ticker, direction, entry_price, stop_loss,
               target_1, target_2, position_size_pct, regime_state, signal_params
        FROM execution_signals
        WHERE workspace_id = %s AND signal_date = %s
        ORDER BY strategy_id, position_size_pct DESC, ticker
    ''', (workspace_id, run_date))
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        # Cast Decimal → float for all numeric columns
        for col in ('entry_price', 'stop_loss', 'target_1', 'target_2', 'position_size_pct'):
            if d.get(col) is not None:
                d[col] = float(d[col])
        rows.append(d)
    conn.close()
    return rows


def parse_params(raw):
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or '{}')
    except Exception:
        return {}


# ── Build strategy memo ───────────────────────────────────────────────────────

def build_strategy_memo(result, signals, run_date):
    regime     = result.get('regime', 'UNKNOWN')
    n_strats   = result.get('strategies_run', 0)
    n_signals  = len(signals)
    confluence = result.get('confluence_count', 0)
    duration   = result.get('duration_s', 0)

    # Group by strategy
    by_strat = {}
    for s in signals:
        by_strat.setdefault(s['strategy_id'], []).append(s)

    # Strategy summary table
    # Dynamically pull approved strategies from DB for this run date
    try:
        conn2 = psycopg2.connect(os.environ.get('POSTGRES_URI',''))
        cur2  = conn2.cursor()
        cur2.execute(
            "SELECT DISTINCT strategy_id FROM execution_signals WHERE signal_date = %s",
            (run_date,)
        )
        all_approved = [r[0] for r in cur2.fetchall()]
        conn2.close()
    except Exception:
        all_approved = list(by_strat.keys())
    status_lines = []
    for sid in all_approved:
        label  = STRATEGY_LABELS.get(sid, sid)
        count  = len(by_strat.get(sid, []))
        bullet = '✅' if count > 0 else '⏸'
        reason = ''
        if count == 0:
            reason = ' (no signals this cycle)'
        status_lines.append(f'{bullet} {label}: {count} signals{reason}')

    total_exposure = sum(float(s['position_size_pct']) for s in signals) * 100

    lines = [
        f'📡 **Strategy Execution Memo — {run_date}**',
        f'Regime: **{regime}** | Strategies run: {n_strats} | Signals: {n_signals} | Confluence: {confluence} | Runtime: {duration:.2f}s',
        '',
        '**Strategy Status**',
    ] + status_lines + ['']

    for sid, sigs in by_strat.items():
        label = STRATEGY_LABELS.get(sid, sid)
        p_sample = parse_params(sigs[0]['signal_params'])
        subheader = ''
        if 'spy_beats_cash' in p_sample:
            subheader = f" — SPY beats T-bill: {'YES' if p_sample['spy_beats_cash'] else 'NO'}"
        elif 'lookback_days' in p_sample:
            subheader = f" — {p_sample['lookback_days']}d lookback, skip {p_sample.get('skip_days',21)}d"

        lines.append(f'**{label}**{subheader}')
        lines.append('```')
        lines.append('Ticker  Entry       Stop      Risk      T1       R:R   Size    12mo')
        lines.append('─' * 66)
        for s in sigs:
            p       = parse_params(s['signal_params'])
            mom     = p.get('lookback_ret', p.get('momentum_12mo', 0))
            risk    = (s['entry_price'] - s['stop_loss']) / s['entry_price'] * 100
            rr      = (s['target_1'] - s['entry_price']) / max(s['entry_price'] - s['stop_loss'], 0.01)
            sz      = float(s['position_size_pct']) * 100
            mom_str = f"{'+' if mom >= 0 else ''}{mom*100:.0f}%"
            lines.append(
                f"{s['ticker']:<6}  "
                f"${s['entry_price']:>8.2f}  "
                f"${s['stop_loss']:>8.2f}  "
                f"{risk:>4.1f}%  "
                f"${s['target_1']:>8.2f}  "
                f"{rr:>4.1f}x  "
                f"{sz:>4.2f}%  "
                f"{mom_str:>7}"
            )
        lines.append('```')
        lines.append('')

    lines.append(f'**Total gross exposure: {total_exposure:.2f}%** (HIGH_VOL scale=0.35, 35% of full sizing)')
    return '\n'.join(lines)


# ── Build research synthesis ──────────────────────────────────────────────────

def build_signal_synthesis(result, signals, run_date):
    regime = result.get('regime', 'UNKNOWN')

    # Score each signal for conviction
    scored = []
    for s in signals:
        p    = parse_params(s['signal_params'])
        mom  = abs(p.get('lookback_ret', p.get('momentum_12mo', 0)))
        rank = p.get('momentum_rank', 0.5)
        rr   = (s['target_1'] - s['entry_price']) / max(s['entry_price'] - s['stop_loss'], 0.01)
        risk = (s['entry_price'] - s['stop_loss']) / s['entry_price']
        score = mom * rr * float(float(s['position_size_pct'])) * (1 + rank)
        scored.append({**s, '_mom': mom, '_rank': rank, '_rr': rr, '_risk': risk, '_score': score, '_p': p})
    scored.sort(key=lambda x: x['_score'], reverse=True)

    # Cross-strategy confluence
    ticker_strats = {}
    for s in signals:
        ticker_strats.setdefault(s['ticker'], []).append(s['strategy_id'])
    confluent = [(t, strats) for t, strats in ticker_strats.items() if len(strats) >= 2]

    # By-strategy sizing
    by_strat = {}
    for s in signals:
        by_strat.setdefault(s['strategy_id'], {'count': 0, 'total': 0.0})
        by_strat[s['strategy_id']]['count'] += 1
        by_strat[s['strategy_id']]['total'] += float(s['position_size_pct'])

    regime_notes = {
        'HIGH_VOL':      '⚠️ HIGH_VOL — scale=0.35 (35% of normal sizing). Wide stops in this regime; monitor daily.',
        'TRANSITIONING': '⚡ TRANSITIONING — vol expanding. Tighten stops on failed breaks.',
        'LOW_VOL':       '✅ LOW_VOL — scale=1.0 (full sizing). Trend-following has edge here.',
    }

    lines = [
        f'🔬 **ResearchDesk Signal Synthesis — {run_date}**',
        f'Reading {len(signals)} signals from #strategy-memos | Regime: **{regime}**',
        '',
        '**Top Picks by Conviction** *(momentum × R:R × size × rank)*',
        '```',
        f"{'Ticker':<6} {'Strategy':<30} {'12mo':>7} {'R:R':>5} {'Risk':>6} {'Size':>6}",
        '─' * 62,
    ]
    for s in scored[:5]:
        mom_str  = f"+{s['_mom']*100:.0f}%"
        risk_str = f"{s['_risk']*100:.1f}%"
        rr_str   = f"{s['_rr']:.1f}x"
        sz_str   = f"{float(s['position_size_pct'])*100:.2f}%"
        strat    = STRATEGY_LABELS.get(s['strategy_id'], s['strategy_id'])[:28]
        lines.append(f"{s['ticker']:<6} {strat:<30} {mom_str:>7} {rr_str:>5} {risk_str:>6} {sz_str:>6}")
    lines.append('```')
    lines.append('')

    # Confluence
    if confluent:
        lines.append('**Cross-Strategy Confluence** *(same ticker, ≥2 strategies)*')
        for ticker, strats in confluent:
            short = [s.split('_')[0] + '_' + '_'.join(s.split('_')[1:3]) for s in strats]
            lines.append(f'• **{ticker}** — {" + ".join(short)}')
        lines.append('')
    else:
        lines.append('**Cross-Strategy Confluence:** None today')
        lines.append('')

    # Regime context
    lines.append(f'**Regime Context:** {regime_notes.get(regime, regime)}')
    lines.append('')

    # Wide stop flags
    wide = [s for s in scored if s['_risk'] > 0.08]
    if wide:
        lines.append('**Wide Stop Flags** *(>8% stop distance — consider half-size)*')
        for s in wide:
            lines.append(f"• {s['ticker']} — {s['_risk']*100:.1f}% to stop | T1 R:R {s['_rr']:.1f}x")
        lines.append('')

    # Sizing by strategy
    lines.append('**Sizing by Strategy**')
    for sid, v in by_strat.items():
        label = STRATEGY_LABELS.get(sid, sid)
        avg   = v['total'] / v['count'] * 100
        total = v['total'] * 100
        lines.append(f"• {label}: {v['count']} pos × {avg:.2f}% avg = **{total:.2f}% gross**")

    total_exposure = sum(float(s['position_size_pct']) for s in signals) * 100
    lines.append(f'\n**Portfolio total gross: {total_exposure:.2f}%**')

    return '\n'.join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=str(date.today()))
    args = parser.parse_args()

    run_date     = args.date
    postgres_uri = os.environ.get('POSTGRES_URI', '')
    workspace_id = os.environ.get('WORKSPACE_ID', '')

    if not postgres_uri:
        print('[post_memos] POSTGRES_URI not set')
        sys.exit(1)

    # 1. Run engine
    print(f'[post_memos] Running execution engine for {run_date}...')
    result = run_engine()
    print(f'[post_memos] Engine result: {result}')

    # 2. Query signals
    if not workspace_id:
        # Derive workspace_id from DB
        conn = psycopg2.connect(postgres_uri)
        cur  = conn.cursor()
        cur.execute('SELECT DISTINCT workspace_id FROM execution_signals WHERE signal_date = %s LIMIT 1', (run_date,))
        row = cur.fetchone()
        workspace_id = row[0] if row else 'default'
        conn.close()

    signals = query_signals(postgres_uri, workspace_id, run_date)
    print(f'[post_memos] {len(signals)} signals for {run_date}')

    if not signals:
        print('[post_memos] No signals — nothing to post')
        sys.exit(0)

    # 3. Build and post strategy memo (DataBot → #strategy-memos)
    # Discord post is for human visibility only.
    # pipeline_orchestrator.py calls research_report.py directly after this step completes.
    memo = build_strategy_memo(result, signals, run_date)
    print('\n' + '='*60)
    print('STRATEGY MEMO:')
    print(memo)
    print('='*60)
    wh_post('databot_strategy_memos', memo)

    print('[post_memos] Done — pipeline_orchestrator.py will call research_report.py next.')

    # ── SO-6 lint + memos handoff ─────────────────────────────────────────
    if _HANDOFF_AVAILABLE:
        by_strat = {}
        for s in signals:
            by_strat.setdefault(s['strategy_id'], []).append(s)

        all_memos = {}
        lint_failures = []
        conn2 = psycopg2.connect(postgres_uri)
        now_ts = datetime.utcnow().isoformat() + '+00:00'

        for sid, sigs in by_strat.items():
            params0 = parse_params(sigs[0]['signal_params'])
            memo = {
                'strategy_id':  sid,
                'run_timestamp': now_ts,
                'cycle_date':   run_date,
                'sharpe':       params0.get('sharpe'),
                'max_drawdown': params0.get('max_drawdown'),
                'signal_count': len(sigs),
                'top_signals':  [
                    {'signal_id': str(i), 'ticker': s['ticker'],
                     'direction': s['direction'], 'ev': None, 'kelly': None,
                     'entry': s['entry_price'], 'stop': s['stop_loss'],
                     'target': s['target_1']}
                    for i, s in enumerate(sigs)
                ],
            }
            ok, missing = lint_memo(memo)
            all_memos[sid] = {**memo, 'lint_ok': ok}
            if not ok:
                lint_failures.append((sid, missing))
                try:
                    write_veto_rows(conn2, run_date, sid, missing)
                    conn2.commit()
                except Exception as e:
                    print(f'[post_memos] veto_log write failed ({sid}): {e}')

        conn2.close()

        write_handoff(run_date, 'memos', {
            'run_date':   run_date,
            'strategies': [
                {'strategy_id': sid,
                 'sharpe':       m.get('sharpe'),
                 'max_drawdown': m.get('max_drawdown'),
                 'signal_count': m.get('signal_count', 0),
                 'top_signals':  [s['ticker'] for s in m.get('top_signals', [])],
                 'lint_ok':      m.get('lint_ok', False)}
                for sid, m in all_memos.items()
            ],
            'lint_failures': [[s, missing] for s, missing in lint_failures],
        })
        print(f'[post_memos] Memos handoff written — {len(lint_failures)} lint failure(s)')
