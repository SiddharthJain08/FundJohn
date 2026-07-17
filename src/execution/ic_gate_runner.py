#!/usr/bin/env python3
"""Phase 2A — Renaissance IC Approval Gate: orchestrator step entry.

Pipeline position: BETWEEN `signals` (engine.py → execution_signals) and
`handoff` (trade_handoff_builder.py). Reads today's signals from the same
table the handoff builder reads, runs the pure-function classifier
(`ic_gate.classify_signals`), persists every decision to `ic_decisions`,
and (for IC_REQUIRED rows) posts a single consolidated Discord prompt
to #ic-approvals + polls the table for operator responses until a
configurable timeout.

Default-OFF behavior: early-exit unless OPENCLAW_IC_GATE=1 — production
must be byte-identical to today when the gate is unset. The orchestrator
wrapper (pipeline_orchestrator.py) ALSO catches exceptions from this
script and continues fail-open, mirroring the tradejohn_confirmer pattern
inside regime_blended_sizer_live.py.

Decision flow for one signal:
  classifier → AUTO_APPROVE  → insert + done
  classifier → VETOED        → insert + done (decided_by='classifier')
  classifier → IC_REQUIRED   → insert pending row,
                                post consolidated Discord prompt,
                                poll ic_decisions until operator UPDATEs
                                  classification → APPROVED/SCALED/VETOED
                                or IC_TIMEOUT_SECONDS elapses
                                (then re-mark as TIMED_OUT — treated as VETO
                                downstream, fail-SAFE).

Spec: docs/archive/superpowers/plans/2026-05-15-fincept-imports-phase-2-master-plan.md
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = ROOT / 'src'
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# ── Tunables ─────────────────────────────────────────────────────────────────
DEFAULT_TIMEOUT_SECONDS  = int(os.environ.get('IC_TIMEOUT_SECONDS', '600'))
POLL_INTERVAL_SECONDS    = int(os.environ.get('IC_POLL_INTERVAL', '30'))
DISCORD_CHANNEL_KEY      = 'ic-approvals'
DISCORD_AGENT            = 'botjohn'
WEBHOOK_HELPER           = ROOT / 'src' / 'agent' / 'curators' / '_discord_webhook.js'
MANIFEST_PATH            = ROOT / 'src' / 'strategies' / 'manifest.json'


# ── DB helpers ──────────────────────────────────────────────────────────────

def _db_uri() -> str:
    return (os.environ.get('DATABASE_URL')
            or os.environ.get('POSTGRES_URI')
            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def _connect():
    import psycopg2
    return psycopg2.connect(_db_uri())


def _load_manifest() -> dict:
    """Manifest is on disk (file-system source of truth for lifecycle.state)."""
    try:
        return json.loads(MANIFEST_PATH.read_text())
    except Exception as e:
        logger.warning('ic_gate_runner: manifest load failed (%s); '
                       'every signal will be treated as unknown_strategy → VETOED', e)
        return {'strategies': {}}


def _load_signals(run_date: str) -> list[dict]:
    """Read the same signal columns the handoff builder reads — keeps the
    IC gate operating on the exact handoff input set."""
    import psycopg2.extras
    conn = _connect()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT strategy_id, ticker, direction, entry_price, stop_loss,
                   target_1, target_2, target_3, position_size_pct,
                   signal_params, regime_state
              FROM execution_signals
             WHERE signal_date = %s
             ORDER BY strategy_id, ticker
        """, (run_date,))
        out: list[dict] = []
        for r in cur.fetchall():
            d = dict(r)
            for col in ('entry_price', 'stop_loss', 'target_1', 'target_2',
                        'target_3', 'position_size_pct'):
                v = d.get(col)
                if v is not None:
                    try:
                        d[col] = float(v)
                    except (TypeError, ValueError):
                        d[col] = None
            sp = d.get('signal_params')
            if isinstance(sp, str):
                try:
                    d['signal_params'] = json.loads(sp) if sp else {}
                except ValueError:
                    d['signal_params'] = {}
            out.append(d)
        return out
    finally:
        conn.close()


def _persist_decisions(run_date: str, paired: list[dict]) -> dict[str, int]:
    """Insert one row per (signal, decision). Returns counts by classification."""
    import psycopg2.extras
    if not paired:
        return {}
    counts: dict[str, int] = {}
    rows = []
    for item in paired:
        sig = item['signal'] or {}
        dec = item['decision'] or {}
        classification = dec.get('classification', 'VETOED')
        counts[classification] = counts.get(classification, 0) + 1
        decided_by = 'classifier' if classification in ('AUTO_APPROVE', 'VETOED') else None
        decided_at = datetime.now(timezone.utc) if decided_by else None
        rows.append((
            run_date,
            str(sig.get('strategy_id') or '__missing__'),
            str(sig.get('ticker') or '__missing__'),
            classification,
            dec.get('reason'),
            decided_by,
            decided_at,
            dec.get('scaled_size_pct'),
            json.dumps(_jsonable(sig)),
        ))
    conn = _connect()
    try:
        cur = conn.cursor()
        psycopg2.extras.execute_batch(cur, """
            INSERT INTO ic_decisions
                (run_date, strategy_id, ticker, classification, reason,
                 decided_by, decided_at, scaled_size_pct, raw_signal)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        """, rows, page_size=100)
        conn.commit()
    finally:
        conn.close()
    return counts


def _jsonable(obj):
    """Best-effort coercion for raw_signal JSONB (handle Decimal, datetime)."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    return str(obj)


# ── Discord ──────────────────────────────────────────────────────────────────

def _format_prompt(run_date: str, pending: list[dict]) -> str:
    """Tabular consolidated prompt for #ic-approvals."""
    lines = [f'**IC approval requested — {run_date}** ({len(pending)} signals)',
             '```',
             f'{"#":>2}  {"strategy":<40}  {"ticker":<6}  {"dir":<5}  {"reason"}',]
    for n, item in enumerate(pending, start=1):
        sig = item['signal']
        dec = item['decision']
        sid = (sig.get('strategy_id') or '?')[:40]
        tkr = (sig.get('ticker') or '?')[:6]
        d   = (sig.get('direction') or '?')[:5]
        rsn = (dec.get('reason') or '')[:40]
        lines.append(f'{n:>2}  {sid:<40}  {tkr:<6}  {d:<5}  {rsn}')
    lines.append('```')
    lines.append('Commands: `approve N` / `veto N` / `scale N PCT` '
                 '(PCT is a 0..100 percentage, clamped). '
                 f'Auto-veto in {DEFAULT_TIMEOUT_SECONDS}s if no response.')
    return '\n'.join(lines)


def _post_discord(text: str) -> dict:
    """Shell out to Node webhook helper. Non-fatal — return status dict."""
    if not WEBHOOK_HELPER.exists():
        return {'ok': False, 'reason': 'webhook_helper_missing'}
    js = (
        "const { postToChannel } = require(%r);"
        "let chunks=[];process.stdin.on('data',d=>chunks.push(d));"
        "process.stdin.on('end',async()=>{const text=Buffer.concat(chunks).toString('utf8');"
        "const r=await postToChannel(%r,%r,text);"
        "console.log(JSON.stringify(r));});"
    ) % (str(WEBHOOK_HELPER), DISCORD_AGENT, DISCORD_CHANNEL_KEY)
    try:
        proc = subprocess.run(
            ['node', '-e', js],
            input=text.encode('utf-8'),
            capture_output=True, timeout=30,
            env={**os.environ},
        )
        if proc.returncode != 0:
            return {'ok': False, 'reason': 'node_nonzero',
                    'stderr': proc.stderr.decode('utf-8', 'replace')[:500]}
        out = proc.stdout.decode('utf-8', 'replace').strip().splitlines()
        if not out:
            return {'ok': False, 'reason': 'no_output'}
        try:
            return json.loads(out[-1])
        except json.JSONDecodeError:
            return {'ok': False, 'reason': 'unparseable', 'raw': out[-1][:200]}
    except Exception as e:
        return {'ok': False, 'reason': 'spawn_failed', 'error': str(e)}


# ── Poll loop ────────────────────────────────────────────────────────────────

def _poll_for_decisions(run_date: str, timeout_seconds: int,
                        poll_interval: int) -> dict[str, int]:
    """Block until every IC_REQUIRED row for run_date has a non-null
    decided_at, or until timeout. Returns final counts by classification."""
    deadline = time.monotonic() + timeout_seconds
    last_pending = -1
    while time.monotonic() < deadline:
        conn = _connect()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT classification, COUNT(*)
                  FROM ic_decisions
                 WHERE run_date = %s
                   AND classification IN ('IC_REQUIRED','APPROVED','SCALED','VETOED','TIMED_OUT')
                 GROUP BY classification
            """, (run_date,))
            counts = {k: int(v) for k, v in cur.fetchall()}
            pending = counts.get('IC_REQUIRED', 0)
            if pending == 0:
                return counts
            if pending != last_pending:
                logger.info('ic_gate_runner: %d signals still awaiting operator decision', pending)
                last_pending = pending
        finally:
            conn.close()
        time.sleep(poll_interval)

    # Timeout: anything still IC_REQUIRED → TIMED_OUT (fail-SAFE = treat as VETO).
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE ic_decisions
               SET classification='TIMED_OUT',
                   decided_by='runner_timeout',
                   decided_at=NOW(),
                   reason=COALESCE(reason,'')||' [auto-vetoed: no operator response within timeout]'
             WHERE run_date=%s AND classification='IC_REQUIRED'
        """, (run_date,))
        n = cur.rowcount
        conn.commit()
        logger.warning('ic_gate_runner: timed out — %d signals auto-vetoed', n)
        cur.execute("""
            SELECT classification, COUNT(*) FROM ic_decisions
             WHERE run_date=%s GROUP BY classification
        """, (run_date,))
        return {k: int(v) for k, v in cur.fetchall()}
    finally:
        conn.close()


# ── Top-level run ────────────────────────────────────────────────────────────

def run(run_date: str, *, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        poll_interval: int = POLL_INTERVAL_SECONDS) -> dict:
    """Top-level entry. Caller is responsible for the env-gate check
    (kept on the caller so a downstream importer can call run() directly
    in tests without the env gate firing)."""
    from execution import ic_gate

    manifest = _load_manifest()
    signals = _load_signals(run_date)
    if not signals:
        return {'status': 'no_signals', 'run_date': run_date}

    paired = ic_gate.classify_signals(signals, manifest)
    counts = _persist_decisions(run_date, paired)

    pending = [p for p in paired if p['decision']['classification'] == ic_gate.IC_REQUIRED]
    if not pending:
        return {'status': 'no_ic_required', 'run_date': run_date,
                'counts': counts, 'n_signals': len(signals)}

    prompt = _format_prompt(run_date, pending)
    post_result = _post_discord(prompt)
    if not post_result.get('ok'):
        # Discord post failed — operator can't approve. Log + still poll the
        # table in case decisions arrive via another path (e.g. dashboard).
        logger.warning('ic_gate_runner: Discord post failed: %s', post_result)

    final_counts = _poll_for_decisions(run_date,
                                       timeout_seconds=timeout_seconds,
                                       poll_interval=poll_interval)
    return {'status': 'ok', 'run_date': run_date,
            'initial_counts': counts, 'final_counts': final_counts,
            'discord_post': post_result, 'n_signals': len(signals),
            'n_ic_required': len(pending)}


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    p = argparse.ArgumentParser()
    p.add_argument('--date', required=False,
                   default=datetime.now(timezone.utc).date().isoformat())
    p.add_argument('--timeout-seconds', type=int, default=DEFAULT_TIMEOUT_SECONDS)
    p.add_argument('--poll-interval', type=int, default=POLL_INTERVAL_SECONDS)
    p.add_argument('--dry-run', action='store_true',
                   help='Skip DB writes + Discord post; only run the classifier and log.')
    args = p.parse_args()

    # Hard default-OFF check — do NOT touch DB / Discord / manifest when unset.
    from execution import ic_gate
    if not ic_gate.is_enabled():
        print('[ic-gate] OPENCLAW_IC_GATE != 1 — skipping (default-OFF)')
        return 0

    try:
        if args.dry_run:
            manifest = _load_manifest()
            signals = _load_signals(args.date)
            paired = ic_gate.classify_signals(signals, manifest)
            counts: dict[str, int] = {}
            for p2 in paired:
                c = p2['decision']['classification']
                counts[c] = counts.get(c, 0) + 1
            print(f'[ic-gate] DRY-RUN {args.date}: '
                  f'n_signals={len(signals)} counts={counts}')
            return 0
        result = run(args.date,
                     timeout_seconds=args.timeout_seconds,
                     poll_interval=args.poll_interval)
        print(f'[ic-gate] {args.date}: {result}')
        return 0
    except Exception as exc:
        # Fail-OPEN per spec: never block the cycle if the IC gate itself
        # explodes. Print full traceback for postmortem; orchestrator wrapper
        # also catches at its level.
        print(f'[ic-gate] WARN: runner failed (non-fatal, fail-open): {exc!s}')
        traceback.print_exc()
        return 0


if __name__ == '__main__':
    sys.exit(main())
