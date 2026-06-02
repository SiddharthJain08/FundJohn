#!/usr/bin/env python3
"""Weekly chronic-fold report: strategy groups that fold together repeatedly.
Posts a recommendation digest to #strategy-memos for MANUAL retirement decisions.
Feeds the future Tier-3 monthly convergence process (spec §10).

MOD-B note: there is no DISCORD_STRATEGY_MEMOS_WEBHOOK env var in the codebase.
The canonical pattern (see send_report.py, regime_liquidator.py) is to look up
the webhook URL from agent_registry.webhook_urls using channel key 'strategy-memos'
(owned by the mastermind persona). The env var DISCORD_STRATEGY_MEMOS_WEBHOOK is
supported as a direct-URL override for standalone/testing usage only.
"""
from __future__ import annotations

import os
from collections import defaultdict


_DOTENV_LOADED = False


def _db():
    global _DOTENV_LOADED
    import psycopg2
    if not _DOTENV_LOADED:
        from dotenv import load_dotenv
        load_dotenv()
        _DOTENV_LOADED = True
    return psycopg2.connect(os.environ.get('DATABASE_URL')
                            or os.environ.get('POSTGRES_URI')
                            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def _get_strategy_memos_webhook() -> str | None:
    """Look up the #strategy-memos webhook URL from agent_registry.
    Mirrors the pattern in send_report.py / regime_liquidator.py.
    Falls back to DISCORD_STRATEGY_MEMOS_WEBHOOK env var for standalone use."""
    # Direct env var override (for standalone / CI usage)
    direct = os.environ.get('DISCORD_STRATEGY_MEMOS_WEBHOOK')
    if direct:
        return direct

    # Canonical path: look up persisted webhook from agent_registry
    try:
        conn = _db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT webhook_urls FROM agent_registry WHERE webhook_urls IS NOT NULL"
                )
                for (hooks,) in cur.fetchall():
                    if hooks and 'strategy-memos' in hooks:
                        return hooks['strategy-memos']
        finally:
            conn.close()
    except Exception as e:
        print(f'[fold_report] webhook lookup failed: {e}')
    return None


def chronic_folds(min_runs: int = 3, lookback_days: int = 35) -> list[dict]:
    """Groups (by sorted strategy_ids) appearing in >= min_runs distinct audit runs."""
    sql = """
        SELECT regime_state, strategy_ids, representative, member_sharpes, run_at::date
          FROM strategy_fold_audit
         WHERE run_at >= CURRENT_DATE - (%s::int * INTERVAL '1 day')
    """
    runs_by_key: dict[tuple, set] = defaultdict(set)
    meta: dict[tuple, dict] = {}

    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (lookback_days,))
            for regime, sids, rep, sharpes, run_date in cur.fetchall():
                key = (regime, tuple(sorted(sids)))
                runs_by_key[key].add(run_date)
                meta[key] = {
                    'regime': regime,
                    'strategy_ids': sorted(sids),
                    'representative': rep,
                    'member_sharpes': sharpes,
                }
    finally:
        conn.close()

    return [
        {**meta[k], 'runs': len(v)}
        for k, v in runs_by_key.items()
        if len(v) >= min_runs
    ]


def format_report(rows: list[dict]) -> str:
    if not rows:
        return 'No chronic fold-groups this week.'
    lines = ['**Chronic fold-groups** (candidates for manual retirement):']
    for r in rows:
        members = ', '.join(
            f"{s}({(r['member_sharpes'] or {}).get(s, '?')})"
            for s in r['strategy_ids']
        )
        lines.append(
            f"- [{r['regime']}] {members} — folded {r['runs']}x -> keep **{r['representative']}**"
        )
    return '\n'.join(lines)


def main():
    rows = chronic_folds()
    msg = format_report(rows)
    print(msg)

    if os.environ.get('OPENCLAW_FOLD_REPORT_POST') == '1':
        import json
        import urllib.request
        import urllib.error
        url = _get_strategy_memos_webhook()
        if url:
            try:
                req = urllib.request.Request(
                    url,
                    data=json.dumps({'content': msg[:1900]}).encode(),
                    # Discord proxies through Cloudflare, which 403s the default
                    # python-urllib/* User-Agent with error code 1010. Send an
                    # explicit UA so the request passes the edge filter.
                    headers={'Content-Type': 'application/json',
                             'User-Agent': 'OpenClaw-FoldReport/1.0 (+botjohn)'},
                )
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                print(f'fold_report: webhook post failed ({e})')
        else:
            print('[fold_report] OPENCLAW_FOLD_REPORT_POST=1 but no webhook URL found '
                  '(set DISCORD_STRATEGY_MEMOS_WEBHOOK or seed agent_registry.webhook_urls)')
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
