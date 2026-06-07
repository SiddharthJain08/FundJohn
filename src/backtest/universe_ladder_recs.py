"""SP-7 Phase B — recommendation persistence + Discord posting (python side).

Mirrors the JS plumbing contracts exactly:
- row shape: universe_recommender.js _persist (approved=NULL pending)
- message shape + REQUIRED footer: universe_recommender.js _formatDiscordMessage
- webhook: agent_registry.webhook_urls['universe-recs'] + explicit UA
  (Cloudflare 1010 — see reference_discord_urllib_cloudflare_ua)
"""
from __future__ import annotations

import json
import os
import urllib.request

GRID_COLS = ('sharpe', 'max_dd_pct', 'win_rate', 'trades_n', 'sortino', 'calmar')
USER_AGENT = 'OpenClaw-LadderRecs/1.0 (+botjohn)'


def insert_recommendation(pg, *, strategy_id: str, current_predicate: str,
                          candidate_predicate: str, candidate_set_id: str,
                          backtest_summary: dict, rationale: str) -> int:
    with pg.cursor() as cur:
        cur.execute(
            """INSERT INTO strategy_universe_recommendations
                 (strategy_id, current_predicate, candidate_predicate,
                  candidate_set_id, backtest_summary, rationale, approved,
                  mastermind_cost_usd)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s, NULL, NULL)
               RETURNING id""",
            (strategy_id, current_predicate, candidate_predicate,
             candidate_set_id, json.dumps(backtest_summary), rationale))
        rec_id = cur.fetchone()[0]
    pg.commit()
    return int(rec_id)


def build_rationale(verdict: dict, *, window: tuple[str, str]) -> str:
    parts = [f"sp7b tier-ladder {window[0]}..{window[1]};",
             f"eligible={','.join(verdict['eligible']) or 'none'};"]
    for c in verdict.get('comparisons', []):
        mark = '→' if c['displaced'] else '✗'
        parts.append(f"{c['challenger']} vs {c['incumbent']} Δ={c['delta']:+.2f}{mark};")
    parts.append(f"verdict={verdict['verdict']}"
                 + (f" choice={verdict['choice']}" if verdict['choice'] else ''))
    return ' '.join(parts)[:1000]


def _fmt(v):
    return 'n/a' if v is None else str(v)


def format_change_message(strategy_id: str, current: str, choice: str,
                          rationale: str, grid: list[dict], *, rec_id: int) -> str:
    lines = [
        f'**Universe Rec — {strategy_id}**',
        f'Current: `{current}` → Proposed: `{choice}`',
        'Confidence: deterministic (sp7b ladder)',
        f'Rationale: {rationale[:400]}', '', '**Grid:**',
        '| Candidate | Sharpe | MaxDD% | WinRate | Trades | Sortino | Calmar |',
        '|---|---|---|---|---|---|---|',
    ]
    for row in grid:
        lines.append('| `' + row['name'] + '` | '
                     + ' | '.join(_fmt(row.get(c)) for c in GRID_COLS) + ' |')
    lines.append('')
    lines.append('React ✅ to approve · ❌ to reject · ⏸ to defer')
    lines.append(f'_footer: universe-rec:{rec_id}_')
    return '\n'.join(lines)[:1900]


def format_summary_message(non_changes: list[tuple[str, str]]) -> str:
    """Batched no-change/no-signal/universe-independent verdicts — NOT
    adoptable, so NO universe-rec footer (the reaction parser must not fire)."""
    lines = ['**Universe Ladder — no-change verdicts**']
    for sid, verdict in non_changes:
        lines.append(f'- `{sid}`: {verdict}')
    return '\n'.join(lines)[:1900]


def get_webhook(pg) -> str | None:
    direct = os.environ.get('DISCORD_UNIVERSE_RECS_WEBHOOK')
    if direct:
        return direct
    try:
        with pg.cursor() as cur:
            cur.execute("SELECT webhook_urls FROM agent_registry "
                        "WHERE webhook_urls IS NOT NULL")
            for (hooks,) in cur.fetchall():
                if hooks and 'universe-recs' in hooks:
                    return hooks['universe-recs']
    except Exception as e:
        print(f'[ladder-recs] webhook lookup failed: {e}')
    return None


def post_discord(pg, msg: str) -> bool:
    url = get_webhook(pg)
    if not url:
        print('[ladder-recs] no universe-recs webhook — skipping post')
        return False
    try:
        req = urllib.request.Request(
            url, data=json.dumps({'content': msg[:1900]}).encode(),
            headers={'Content-Type': 'application/json',
                     'User-Agent': USER_AGENT})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f'[ladder-recs] webhook post failed ({e}) — non-fatal')
        return False
