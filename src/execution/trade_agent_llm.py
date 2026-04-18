#!/usr/bin/env python3
"""
trade_agent_llm.py — Invoke TradeJohn Claude agent for daily signal generation.

Reads the enriched research handoff (HV/beta/EV), builds context, calls the
Node.js run-subagent-cli.js wrapper, and exits 0 on success.

Usage:
    python3 src/execution/trade_agent_llm.py [--date YYYY-MM-DD]
"""

import os, sys, json, subprocess, tempfile, psycopg2, psycopg2.extras
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.handoff import read_handoff


# ── Helpers ───────────────────────────────────────────────────────────────────

def build_veto_histogram(postgres_uri, days=30):
    """
    Returns {strategy_id: {veto_reason: count}} for the last N days.
    Returns {} on any error (non-critical).
    """
    if not postgres_uri:
        return {}
    try:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        conn   = psycopg2.connect(postgres_uri)
        cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            '''SELECT strategy_id, veto_reason, COUNT(*) AS cnt
               FROM veto_log
               WHERE run_date >= %s AND strategy_id IS NOT NULL
               GROUP BY strategy_id, veto_reason
               ORDER BY strategy_id, cnt DESC''',
            (cutoff,)
        )
        hist = {}
        for row in cur.fetchall():
            sid    = row['strategy_id']
            reason = row['veto_reason']
            hist.setdefault(sid, {})[reason] = int(row['cnt'])
        conn.close()
        return hist
    except Exception as e:
        print(f'[trade_agent_llm] veto_histogram unavailable: {e}')
        return {}


def load_portfolio_state():
    """
    Returns portfolio state dict from output/portfolio.json, or {} if absent.
    """
    p = ROOT / 'output' / 'portfolio.json'
    try:
        if p.exists():
            return json.loads(p.read_text())
    except Exception as e:
        print(f'[trade_agent_llm] portfolio state unavailable: {e}')
    return {}


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=str(date.today()))
    args = parser.parse_args()

    run_date     = args.date
    postgres_uri = os.environ.get('POSTGRES_URI', '')

    print(f'[trade_agent_llm] Building TradeJohn context for {run_date}...')

    # 1. Read enriched research handoff; fall back to memos handoff
    handoff = read_handoff(run_date, 'research')
    if not handoff:
        print('[trade_agent_llm] Research handoff absent — trying memos handoff')
        handoff = read_handoff(run_date, 'memos')

    if not handoff:
        print('[trade_agent_llm] No handoff available — cannot run TradeJohn')
        sys.exit(1)

    print(f'[trade_agent_llm] Handoff loaded: regime={handoff.get("regime","?")} '
          f'signals={len(handoff.get("signals", handoff.get("strategies", [])))}')

    # 2. Build veto histogram
    veto_histogram = build_veto_histogram(postgres_uri)

    # 3. Load portfolio state
    portfolio_state = load_portfolio_state()

    # 4. Assemble context dict
    ctx = {
        'cycle_date':      run_date,
        'handoff':         handoff,
        'veto_histogram':  veto_histogram,
        'portfolio_state': portfolio_state,
    }

    # 5. Write context to temp file
    tmp = tempfile.NamedTemporaryFile(
        mode='w', suffix='.json', prefix='tradejohn-ctx-', delete=False
    )
    json.dump(ctx, tmp, default=str)
    tmp.close()
    ctx_file = tmp.name

    node_cli = ROOT / 'src' / 'agent' / 'run-subagent-cli.js'

    print(f'[trade_agent_llm] Invoking TradeJohn via {node_cli.name}...')

    try:
        env = {
            **os.environ,
            'PYTHONPATH':    str(ROOT),
            'OPENCLAW_DIR':  str(ROOT),
        }
        result = subprocess.run(
            ['node', str(node_cli),
             '--type',         'tradejohn',
             '--ticker',       run_date,
             '--workspace',    str(ROOT / 'workspaces' / 'default'),
             '--context-file', ctx_file],
            cwd=str(ROOT),
            env=env,
            timeout=420,
        )
        if result.returncode != 0:
            print(f'[trade_agent_llm] TradeJohn exited {result.returncode} — see output above')
            sys.exit(1)

        print('[trade_agent_llm] TradeJohn complete.')

    except subprocess.TimeoutExpired:
        print('[trade_agent_llm] TradeJohn timed out after 420s')
        sys.exit(1)
    except Exception as e:
        print(f'[trade_agent_llm] Error: {e}')
        sys.exit(1)
    finally:
        try:
            os.unlink(ctx_file)
        except Exception:
            pass
