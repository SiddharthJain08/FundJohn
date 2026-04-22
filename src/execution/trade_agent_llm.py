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


def _emit_sized_handoff(run_date, raw_stdout):
    """Parse TradeJohn's stdout → write output/handoffs/<run_date>_sized.json.

    run-subagent-cli prints one JSON envelope per line; the last envelope with
    subtype='success' contains TradeJohn's markdown in `.result`. Inside that
    markdown we expect a fenced ```tradejohn_orders block (see tradejohn.md).
    """
    import re
    from execution.handoff import write_handoff as _write_handoff
    markdown = None
    for line in reversed(raw_stdout.splitlines()):
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            env = json.loads(line)
        except ValueError:
            continue
        if env.get('subtype') == 'success' and isinstance(env.get('result'), str):
            markdown = env['result']
            break
    if not markdown:
        print('[trade_agent_llm] No TradeJohn markdown found — sized handoff skipped')
        return
    m = re.search(r'```tradejohn_orders\s*(\{.*?\})\s*```', markdown, re.DOTALL)
    if not m:
        print('[trade_agent_llm] No tradejohn_orders JSON block — sized handoff skipped')
        return
    try:
        payload = json.loads(m.group(1))
    except ValueError as e:
        print(f'[trade_agent_llm] tradejohn_orders block not valid JSON: {e}')
        return
    payload.setdefault('cycle_date', run_date)
    payload['source']        = 'trade_agent_llm'
    payload['generated_at']  = date.today().isoformat()
    _write_handoff(run_date, 'sized', payload)
    print(f'[trade_agent_llm] Sized handoff written — {len(payload.get("orders", []))} orders, '
          f'{len(payload.get("vetoed", []))} vetoed.')


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=str(date.today()))
    args = parser.parse_args()

    run_date     = args.date
    postgres_uri = os.environ.get('POSTGRES_URI', '')

    print(f'[trade_agent_llm] Building TradeJohn context for {run_date}...')

    # 1. Read the structured handoff (Phase 2 format). Falls back to the old
    #    research handoff while the new cycle is being rolled out, then to
    #    the memos handoff as a last resort. The structured handoff is
    #    dramatically smaller than the old markdown research report, which
    #    prevents TradeJohn from blowing its per-agent budget on large signal
    #    batches (root cause of the 2026-04-22 catch-up failure).
    handoff = read_handoff(run_date, 'structured')
    handoff_stage = 'structured'
    if not handoff:
        handoff = read_handoff(run_date, 'research')
        handoff_stage = 'research' if handoff else None
    if not handoff:
        handoff = read_handoff(run_date, 'memos')
        handoff_stage = 'memos' if handoff else None

    if not handoff:
        print('[trade_agent_llm] No handoff available — cannot run TradeJohn')
        sys.exit(1)

    regime_str = handoff.get('regime')
    if isinstance(regime_str, dict):
        regime_str = regime_str.get('state', '?')
    print(f'[trade_agent_llm] Handoff loaded ({handoff_stage}): regime={regime_str} '
          f'signals={len(handoff.get("signals", handoff.get("strategies", [])))}')

    # 2. Build veto histogram — structured handoff already has it but older
    #    stages don't; skip the DB call when it's embedded.
    veto_histogram = handoff.get('veto_history_30d') or build_veto_histogram(postgres_uri)

    # 3. Load portfolio state — structured handoff has it embedded.
    portfolio_state = handoff.get('portfolio') or load_portfolio_state()

    # 4. Assemble context dict. For the structured stage we pass the handoff
    #    directly (it's already the right shape); for legacy stages we keep
    #    the old wrapper.
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
            capture_output=True,
            text=True,
            timeout=420,
        )
        # Stream output to parent regardless so systemd journal still sees it
        sys.stdout.write(result.stdout or '')
        sys.stderr.write(result.stderr or '')
        if result.returncode != 0:
            print(f'[trade_agent_llm] TradeJohn exited {result.returncode} — see output above')
            sys.exit(1)

        print('[trade_agent_llm] TradeJohn complete.')

        # Extract TradeJohn's markdown from the claude-bin JSON envelope, then
        # pull out the fenced ```tradejohn_orders block and write the sized
        # handoff that alpaca_executor.py will read.
        try:
            _emit_sized_handoff(run_date, result.stdout or '')
        except Exception as exc:
            print(f'[trade_agent_llm] sized-handoff emit failed: {exc}')

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
