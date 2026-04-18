#!/usr/bin/env python3
"""
portfolio_report.py — Step 4 of the trade pipeline.

Fetches open positions from Alpaca + DB signal_pnl, generates a portfolio
status report and heuristic position recommendations, posts both to Discord.

Usage:
  python3 src/execution/portfolio_report.py --date YYYY-MM-DD
"""

import os, sys, json, argparse, logging, uuid
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import requests
import psycopg2, psycopg2.extras
from execution.alpaca_trader import _alpaca_session, get_positions, get_portfolio_history

logging.basicConfig(level=logging.INFO, format='%(asctime)s [PORTFOLIO_REPORT] %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

# ── Constants (mirror trade_agent.py) ─────────────────────────────────────────

HOLDING_PERIOD = {
    'S5_max_pain':              {'min': 1,  'target': 5,  'max': 10},
    'S9_dual_momentum':         {'min': 21, 'target': 63, 'max': 126},
    'S10_quality_value':        {'min': 21, 'target': 42, 'max': 84},
    'S12_insider':              {'min': 5,  'target': 21, 'max': 42},
    'S15_iv_rv_arb':            {'min': 1,  'target': 5,  'max': 10},
    'S_custom_jt_momentum_12mo':{'min': 21, 'target': 63, 'max': 126},
    'S23_regime_momentum':      {'min': 5,  'target': 21, 'max': 42},
    'S24_52wk_high_proximity':  {'min': 5,  'target': 21, 'max': 42},
    'S25_dual_momentum_v2':     {'min': 21, 'target': 63, 'max': 126},
}
DEFAULT_HP = {'min': 1, 'target': 5, 'max': 21}

EXIT_EARLY_LOSS_THRESHOLD    = -0.08   # -8% unrealized loss
EXIT_EARLY_HP_THRESHOLD      = 0.90   # 90% of max holding period elapsed
TAKE_PROFIT_THRESHOLD        = 0.15   # +15% above best target
INCREASE_SIZE_CONFLUENCE_MIN = 2      # confluence_count >= 2 to increase


# ── DB helpers ────────────────────────────────────────────────────────────────

def load_webhook_urls(postgres_uri):
    """Load webhook URLs for tradedesk agent from DB, fallback to env vars."""
    webhooks = {
        'trade-reports':           os.environ.get('TRADEDESK_TRADE_REPORTS_WEBHOOK', ''),
        'position-recommendations': os.environ.get('TRADEDESK_POS_RECS_WEBHOOK', ''),
    }
    try:
        conn = psycopg2.connect(postgres_uri)
        cur  = conn.cursor()
        cur.execute("SELECT webhook_urls FROM agent_registry WHERE id='tradedesk'")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            urls = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            for key in webhooks:
                if urls.get(key):
                    webhooks[key] = urls[key]
    except Exception as e:
        logger.warning(f"Could not load webhook URLs from DB: {e}")
    return webhooks


def load_open_positions_db(postgres_uri, run_date):
    """Load open positions from execution_signals + signal_pnl for run_date."""
    try:
        conn = psycopg2.connect(postgres_uri)
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT
                es.id          AS signal_id,
                es.ticker,
                es.strategy_id,
                es.signal_date,
                es.entry_price,
                es.stop_loss,
                es.target_1,
                es.target_2,
                es.target_3,
                es.confluence_count,
                es.direction,
                sp.days_held,
                sp.unrealized_pnl_pct,
                sp.close_price AS current_price_db
            FROM execution_signals es
            JOIN signal_pnl sp ON sp.signal_id = es.id
            WHERE sp.status = 'open'
              AND sp.pnl_date = %s
        """, [run_date])
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        logger.warning(f"DB position load failed: {e}")
        return []

    positions = []
    for r in rows:
        hp = HOLDING_PERIOD.get(r['strategy_id'], DEFAULT_HP)
        max_hp     = hp['max']
        target_hp  = hp['target']
        days_held  = r['days_held'] or 0
        best_target = (
            float(r['target_3']) if r['target_3'] else
            float(r['target_2']) if r['target_2'] else
            float(r['target_1']) if r['target_1'] else None
        )
        positions.append({
            'signal_id':         str(r['signal_id']),
            'ticker':            r['ticker'],
            'strategy_id':       r['strategy_id'],
            'signal_date':       r['signal_date'].isoformat() if hasattr(r['signal_date'], 'isoformat') else str(r['signal_date']),
            'entry_price':       float(r['entry_price'] or 0),
            'stop_loss':         float(r['stop_loss'] or 0),
            'target_1':          float(r['target_1'] or 0),
            'target_2':          float(r['target_2'] or 0),
            'target_3':          float(r['target_3'] or 0),
            'best_target':       best_target,
            'confluence_count':  r['confluence_count'] or 1,
            'direction':         r['direction'],
            'days_held':         days_held,
            'max_hp':            max_hp,
            'target_hp':         target_hp,
            'days_remaining':    max(0, max_hp - days_held),
            'current_price':     float(r['current_price_db'] or r['entry_price'] or 0),
            'unrealized_pnl_pct': float(r['unrealized_pnl_pct'] or 0),
        })
    return positions


def enrich_with_alpaca(db_positions, alpaca_positions):
    """Overwrite current_price and unrealized_plpc from live Alpaca data."""
    alpaca_map = {p['symbol']: p for p in alpaca_positions}
    for pos in db_positions:
        ap = alpaca_map.get(pos['ticker'])
        if ap:
            pos['current_price']     = ap['current_price']
            pos['unrealized_pnl_pct'] = ap['unrealized_plpc'] * 100  # Alpaca returns fraction
            pos['alpaca_qty']        = ap['qty']
            pos['market_value']      = ap['market_value']
    return db_positions


def save_recommendations(postgres_uri, run_date, recs):
    """Upsert recommendations into position_recommendations table."""
    if not recs:
        return
    try:
        conn = psycopg2.connect(postgres_uri)
        cur  = conn.cursor()
        for rec in recs:
            cur.execute("""
                INSERT INTO position_recommendations
                    (run_date, ticker, strategy_id, action, rationale,
                     entry_price, current_price, unrealized_pnl_pct,
                     days_held, max_hp_days, stop_loss, profit_target)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (run_date, ticker, strategy_id, action) DO NOTHING
                RETURNING id
            """, [
                run_date, rec['ticker'], rec['strategy_id'], rec['action'],
                rec['rationale'], rec['entry_price'], rec['current_price'],
                rec['unrealized_pnl_pct'], rec['days_held'], rec['max_hp'],
                rec.get('stop_loss'), rec.get('profit_target'),
            ])
            row = cur.fetchone()
            rec['db_id'] = str(row[0]) if row else None
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"save_recommendations failed: {e}")


def update_rec_discord_ids(postgres_uri, rec_id, message_id, channel_id):
    try:
        conn = psycopg2.connect(postgres_uri)
        cur  = conn.cursor()
        cur.execute(
            "UPDATE position_recommendations SET discord_message_id=%s, discord_channel_id=%s WHERE id=%s",
            [message_id, channel_id, rec_id]
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"update_rec_discord_ids failed: {e}")


# ── Recommendation engine ─────────────────────────────────────────────────────

def generate_recommendations(positions, run_date):
    """Apply priority rules to produce a recommendation per position."""
    recs = []
    for pos in positions:
        pnl_pct      = pos['unrealized_pnl_pct']
        days_held    = pos['days_held']
        max_hp       = pos['max_hp']
        hp_elapsed   = days_held / max_hp if max_hp > 0 else 0
        best_target  = pos['best_target']
        current      = pos['current_price']
        confluence   = pos['confluence_count']

        action   = 'HOLD'
        rationale = 'Within normal parameters'

        if pnl_pct < EXIT_EARLY_LOSS_THRESHOLD * 100:
            action    = 'EXIT_EARLY'
            rationale = f'Loss threshold breached: {pnl_pct:+.1f}% (limit {EXIT_EARLY_LOSS_THRESHOLD*100:.0f}%)'
        elif hp_elapsed >= EXIT_EARLY_HP_THRESHOLD:
            action    = 'EXIT_EARLY'
            rationale = f'Approaching max hold period: {days_held}/{max_hp} days ({hp_elapsed*100:.0f}%)'
        elif best_target and current > best_target and pnl_pct > TAKE_PROFIT_THRESHOLD * 100:
            action    = 'REDUCE_SIZE'
            rationale = f'Above profit target ${best_target:.2f} with {pnl_pct:+.1f}% gain — take partial profits'
        elif confluence >= INCREASE_SIZE_CONFLUENCE_MIN and pnl_pct > 0:
            action    = 'INCREASE_SIZE'
            rationale = f'{confluence} strategies confluent + {pnl_pct:+.1f}% unrealized — add to winner'

        recs.append({
            'ticker':            pos['ticker'],
            'strategy_id':       pos['strategy_id'],
            'action':            action,
            'rationale':         rationale,
            'entry_price':       pos['entry_price'],
            'current_price':     current,
            'unrealized_pnl_pct': pnl_pct,
            'days_held':         days_held,
            'max_hp':            max_hp,
            'stop_loss':         pos.get('stop_loss'),
            'profit_target':     best_target,
            'signal_id':         pos['signal_id'],
        })
    return recs


# ── Discord message builders ──────────────────────────────────────────────────

def build_portfolio_report_text(equity_data, positions, run_date, n_recs):
    """Build the #trade-reports portfolio status message."""
    eq    = equity_data.get('equity_now', 0)
    dpnl  = equity_data.get('daily_pnl', 0)
    dpct  = equity_data.get('daily_pnl_pct', 0)
    wpnl  = equity_data.get('weekly_pnl', 0)
    wpct  = equity_data.get('weekly_pnl_pct', 0)

    dsign = '+' if dpnl >= 0 else ''
    wsign = '+' if wpnl >= 0 else ''
    demoji = '📈' if dpnl >= 0 else '📉'
    wemoji = '📈' if wpnl >= 0 else '📉'

    lines = [
        f"📊 **TradeDesk — Portfolio Status | {run_date}**",
        "",
        "**Account Summary**",
        f"> Equity: **${eq:,.2f}**",
        f"> Daily P&L: {demoji} **{dsign}${abs(dpnl):,.2f}** ({dpct:+.2f}%)",
        f"> Weekly P&L: {wemoji} **{wsign}${abs(wpnl):,.2f}** ({wpct:+.2f}%)",
        "",
        f"**Open Positions ({len(positions)})**",
    ]

    for pos in positions:
        strat_label = pos['strategy_id'].replace('_', '\\_') if pos['strategy_id'] else '—'
        entry   = pos['entry_price']
        current = pos['current_price']
        pnl_pct = pos['unrealized_pnl_pct']
        stop    = pos.get('stop_loss') or 0
        target  = pos.get('best_target') or pos.get('target_1') or 0
        held    = pos['days_held']
        remain  = pos['days_remaining']
        max_hp  = pos['max_hp']
        pemoji  = '📈' if pnl_pct >= 0 else '📉'
        lines += [
            f"**{pos['ticker']}** ({strat_label})",
            f"> Entry: ${entry:.2f} | Now: ${current:.2f} | P&L: {pemoji} {pnl_pct:+.1f}%",
            f"> Stop: ${stop:.2f} | Target: ${target:.2f}",
            f"> Held: {held}d | {remain}d remaining (max {max_hp}d)",
            "",
        ]

    lines += [
        "─────────────────────────────",
        f"*{n_recs} recommendation(s) posted in #position-recommendations*",
    ]
    return "\n".join(lines)


def build_recommendation_payload(rec):
    """Build a Discord message payload with Approve/Reject buttons."""
    rec_id = rec.get('db_id') or str(uuid.uuid4())
    pnl    = rec['unrealized_pnl_pct']
    pemoji = '📈' if pnl >= 0 else '📉'

    action_labels = {
        'EXIT_EARLY':   '🚪 EXIT EARLY',
        'INCREASE_SIZE':'➕ INCREASE SIZE',
        'REDUCE_SIZE':  '➖ REDUCE SIZE',
        'HOLD':         '⏸️ HOLD',
    }
    label = action_labels.get(rec['action'], rec['action'])

    content = (
        f"**{label} — {rec['ticker']}**\n"
        f"> P&L: {pemoji} {pnl:+.1f}% | Held: {rec['days_held']}d / {rec['max_hp']}d max\n"
        f"> Entry: ${rec['entry_price']:.2f} | Now: ${rec['current_price']:.2f}\n"
        f"> _Rationale: {rec['rationale']}_"
    )

    return {
        'content':    content,
        'components': [
            {
                'type': 1,  # ActionRow
                'components': [
                    {
                        'type':      2,
                        'style':     3,   # SUCCESS (green)
                        'label':     '✅ Approve',
                        'custom_id': f"rec:approve:{rec_id}",
                    },
                    {
                        'type':      2,
                        'style':     4,   # DANGER (red)
                        'label':     '❌ Reject',
                        'custom_id': f"rec:reject:{rec_id}",
                    },
                ],
            }
        ],
    }


def post_to_webhook(webhook_url, payload, wait=False):
    """POST a payload to a Discord webhook. Returns message id if wait=True."""
    if not webhook_url:
        logger.warning("No webhook URL configured — skipping Discord post")
        return None
    params = {'wait': 'true'} if wait else {}
    try:
        r = requests.post(webhook_url, json=payload, params=params, timeout=15)
        if r.ok:
            return r.json().get('id') if wait else None
        logger.warning(f"Webhook POST failed {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"Webhook POST exception: {e}")
    return None


def post_text_to_webhook(webhook_url, text):
    """Post plain text message to webhook, splitting at 2000-char limit."""
    while text:
        chunk = text[:1990]
        text  = text[1990:]
        post_to_webhook(webhook_url, {'content': chunk})


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=date.today().isoformat())
    args = parser.parse_args()
    run_date = args.date

    postgres_uri = os.environ.get('POSTGRES_URI', '')
    if not postgres_uri:
        logger.error("POSTGRES_URI not set")
        sys.exit(1)

    webhooks = load_webhook_urls(postgres_uri)
    tr_hook  = webhooks.get('trade-reports', '')
    pr_hook  = webhooks.get('position-recommendations', '')

    # Build Alpaca session and fetch data
    sess           = _alpaca_session()
    equity_data    = get_portfolio_history(sess)
    alpaca_pos     = get_positions(sess)

    # Load DB positions and enrich with live Alpaca prices
    db_positions   = load_open_positions_db(postgres_uri, run_date)
    positions      = enrich_with_alpaca(db_positions, alpaca_pos)

    logger.info(f"Positions: {len(positions)} open | equity=${equity_data['equity_now']:,.2f}")

    if not positions:
        post_text_to_webhook(tr_hook,
            f"📊 **TradeDesk — Portfolio Status | {run_date}**\n\n*No open positions.*")
        logger.info("No open positions — posted to #trade-reports")
        sys.exit(0)

    # Generate recommendations
    recs        = generate_recommendations(positions, run_date)
    actionable  = [r for r in recs if r['action'] != 'HOLD']

    # Persist recommendations to DB
    save_recommendations(postgres_uri, run_date, recs)

    # Post portfolio report to #trade-reports
    report_text = build_portfolio_report_text(equity_data, positions, run_date, len(actionable))
    post_text_to_webhook(tr_hook, report_text)
    logger.info(f"Portfolio report posted to #trade-reports ({len(positions)} positions, {len(actionable)} recs)")

    # Post actionable recommendations to #position-recommendations with buttons
    if actionable:
        for rec in actionable:
            if not rec.get('db_id'):
                continue
            payload    = build_recommendation_payload(rec)
            message_id = post_to_webhook(pr_hook, payload, wait=True)
            if message_id and rec.get('db_id'):
                update_rec_discord_ids(postgres_uri, rec['db_id'], message_id, 'position-recommendations')
        logger.info(f"{len(actionable)} recommendations posted to #position-recommendations")
    else:
        post_text_to_webhook(pr_hook,
            f"📋 **Position Recommendations | {run_date}**\n\n*All {len(positions)} position(s) at HOLD — no action required.*")
        logger.info("All HOLD — summary posted to #position-recommendations")


if __name__ == '__main__':
    main()
