#!/usr/bin/env python3
"""
trade_agent_llm.py — Invoke TradeJohn Claude agent for daily signal generation.

Reads the enriched research handoff (HV/beta/EV), builds context, calls the
Node.js run-subagent-cli.js wrapper, and exits 0 on success.

Usage:
    python3 src/execution/trade_agent_llm.py [--date YYYY-MM-DD]
"""

import os, sys, json, subprocess, tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.handoff import read_handoff


# ── Helpers ───────────────────────────────────────────────────────────────────

# The 30-day veto histogram was moved to MastermindJohn's weekly runs
# (comprehensive-review + position-recs) where multi-week patterns drive
# strategy memos + sizing deltas. TradeJohn no longer receives it.


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

    # Fold any prefiltered signals from the structured handoff into the
    # sized handoff's `vetoed` list so send_report's digest reflects
    # everything that didn't make it to Alpaca — TradeJohn vetoes AND the
    # prefilter drops. Deduplicates by (ticker, strategy_id) in case a
    # prefiltered signal somehow also appears in TradeJohn's vetoes.
    try:
        from execution.handoff import read_handoff as _read_handoff
        structured = _read_handoff(run_date, 'structured') or {}
        prefiltered = structured.get('prefiltered') or []
        if prefiltered:
            existing = payload.setdefault('vetoed', [])
            seen = {(v.get('ticker'), v.get('strategy_id')) for v in existing}
            for p in prefiltered:
                key = (p.get('ticker'), p.get('strategy_id'))
                if key not in seen:
                    existing.append(p)
                    seen.add(key)
            print(f'[trade_agent_llm] folded {len(prefiltered)} prefiltered signals into vetoed list')
    except Exception as e:
        print(f'[trade_agent_llm] prefilter-fold skipped: {e}')

    _write_handoff(run_date, 'sized', payload)
    print(f'[trade_agent_llm] Sized handoff written — {len(payload.get("orders", []))} orders, '
          f'{len(payload.get("vetoed", []))} vetoed.')

    # Append one row per vetoed entry to veto_log so MastermindJohn's
    # 30-day histogram (comprehensive_review._buildTradePack) stays
    # populated. Zero-token: pure SQL INSERT, no LLM. Captures both
    # TradeJohn's judgement vetoes AND the prefilter folds above.
    _write_veto_log_rows(run_date, payload.get('vetoed') or [])


def _write_veto_log_rows(run_date: str, vetoed: list[dict]) -> None:
    if not vetoed:
        return
    postgres_uri = os.environ.get('POSTGRES_URI', '')
    if not postgres_uri:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(postgres_uri)
        cur  = conn.cursor()
        for v in vetoed:
            reason = v.get('reason') or 'unknown'
            ticker = v.get('ticker')
            strat  = v.get('strategy_id')
            ev     = v.get('ev')
            kelly  = v.get('kelly_final') or v.get('kelly')
            cur.execute(
                '''INSERT INTO veto_log
                     (run_date, strategy_id, ticker, veto_reason, ev, kelly)
                   VALUES (%s, %s, %s, %s, %s, %s)''',
                (run_date, strat, ticker, reason, ev, kelly),
            )
        conn.commit()
        conn.close()
        print(f'[trade_agent_llm] veto_log — {len(vetoed)} row(s) appended')
    except Exception as e:
        print(f'[trade_agent_llm] veto_log write failed: {e}')


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

    # 2. Load portfolio state — structured handoff has it embedded.
    portfolio_state = handoff.get('portfolio') or load_portfolio_state()

    # 3. Assemble TradeJohn's context by whitelisting fields. TradeJohn is a
    #    daily per-signal Kelly-sizing agent; multi-day aggregates
    #    (veto_history_30d, full yesterdays_*) belong to MastermindJohn's
    #    weekly runs, not here. The per-signal `d1` attachments + the
    #    `d1_strategy_stats` rollup give TradeJohn exactly what Rules A–F
    #    need to fire without shipping the full outlier arrays.
    if handoff_stage == 'structured':
        tradejohn_handoff = {
            'cycle_date':        handoff.get('cycle_date') or run_date,
            'generated_at':      handoff.get('generated_at'),
            'regime':            handoff.get('regime'),
            'portfolio':         handoff.get('portfolio'),
            'signals':           handoff.get('signals') or [],
            'sigma_gate':        handoff.get('sigma_gate'),
            'd1_strategy_stats': handoff.get('d1_strategy_stats') or {},
            'mastermind_rec':    handoff.get('mastermind_rec'),
        }
    else:
        # Legacy research / memos stages — pass as-is for backwards compat.
        tradejohn_handoff = handoff

    ctx = {
        'cycle_date':      run_date,
        'handoff':         tradejohn_handoff,
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
            timeout=1500,
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
        print('[trade_agent_llm] TradeJohn timed out after 1500s')
        sys.exit(1)
    except Exception as e:
        print(f'[trade_agent_llm] Error: {e}')
        sys.exit(1)
    finally:
        try:
            os.unlink(ctx_file)
        except Exception:
            pass
