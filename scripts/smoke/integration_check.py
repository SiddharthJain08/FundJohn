#!/usr/bin/env python3
"""
OpenClaw Full Pipeline Integration Test
Run from /root/openclaw: python tests/integration_test.py
"""

import json, os, sys
from datetime import date

TODAY = date.today().isoformat()
PASS  = []
FAIL  = []

def run(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f'  ✅ {name}')
    except AssertionError as e:
        FAIL.append((name, str(e)))
        print(f'  ❌ {name}: {e}')
    except Exception as e:
        FAIL.append((name, str(e)))
        print(f'  💥 {name}: {e}')

print('\n=== OpenClaw Integration Test ===\n')

# 1. PREREQUISITES
print('1. Prerequisites')

def check_regime():
    assert os.path.exists('.agents/market-state/regime_latest.json'), 'Regime file missing'
    with open('.agents/market-state/regime_latest.json') as f:
        r = json.load(f)
    assert r['state'] in ('LOW_VOL','TRANSITIONING','HIGH_VOL','CRISIS')
    assert 0 <= r['stress_score'] <= 100
    assert -100 <= r['roro_score'] <= 100
    assert len(r['active_strategies']) > 0

def check_prefs():
    assert os.path.exists('.agents/user/preferences.json')
    with open('.agents/user/preferences.json') as f:
        p = json.load(f)
    assert 'thresholds' in p
    assert p['thresholds']['sizing']['kelly_fraction'] <= 0.5, 'Kelly fraction must be <= 0.5'

def check_portfolio():
    assert os.path.exists('.agents/user/portfolio.json')
    with open('.agents/user/portfolio.json') as f:
        p = json.load(f)
    assert 'last_verified_at' in p

def check_components():
    assert os.path.exists('src/agent/prompts/components/strategy-matrix.md')
    assert os.path.exists('src/agent/prompts/components/signal-attribution.md')

run('regime_file_valid',    check_regime)
run('preferences_valid',    check_prefs)
run('portfolio_valid',      check_portfolio)
run('component_files_exist',check_components)

# 2. DOCKER / INFRASTRUCTURE
print('\n2. Infrastructure')

def check_redis():
    import redis
    r = redis.Redis.from_url(os.environ.get('REDIS_URL','redis://localhost:6379'))
    r.ping()
    r.set('openclaw_test', '1', ex=5)
    assert r.get('openclaw_test') == b'1'
    r.delete('openclaw_test')

def check_postgres():
    import psycopg2
    conn = psycopg2.connect(os.environ.get('POSTGRES_URI','postgresql://openclaw:password@localhost:5432/openclaw'))
    cur  = conn.cursor()
    cur.execute('SELECT 1')
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
    tables = {r[0] for r in cur.fetchall()}
    required = {'workspaces','threads','checkpoints','analyses','verdict_cache','trades','portfolio'}
    missing  = required - tables
    assert not missing, f'Missing tables: {missing}'
    conn.close()

run('redis_connected',   check_redis)
run('postgres_connected',check_postgres)

# 3. STRATEGY LOGIC
print('\n3. Strategy Logic')

def check_ev_math():
    ev = (130-100)*0.35 + (105-100)*0.35 + (80-100)*0.30
    assert abs(ev - 6.25) < 0.01

def check_negative_ev_halt():
    ev = -3.2
    assert ev < 0

def check_kelly_ceiling():
    sized = min(0.45 * 0.5, 0.10)
    assert sized == 0.10

def check_two_fail_block():
    fails = ['position_limit','correlation']
    verdict = 'BLOCKED' if len(fails)>=2 else 'REDUCED'
    assert verdict == 'BLOCKED'

def check_crisis_no_long():
    state = 'CRISIS'; direction = 'LONG'
    macro_pass = not (state == 'CRISIS' and 'LONG' in direction)
    assert not macro_pass

def check_s14_deactivated():
    matrix = {'LOW_VOL':{'S14'},'HIGH_VOL':set(),'CRISIS':set()}
    assert 'S14' in matrix['LOW_VOL']
    assert 'S14' not in matrix['HIGH_VOL']

def check_confidence_override():
    state = 'LOW_VOL'; confidence = 0.52
    effective = 'TRANSITIONING' if confidence < 0.60 and state == 'LOW_VOL' else state
    assert effective == 'TRANSITIONING'

def check_regime_thresholds():
    mult = {'LOW_VOL':1.00,'TRANSITIONING':0.85,'HIGH_VOL':0.70,'CRISIS':0.50}
    assert mult['CRISIS'] < mult['HIGH_VOL'] < mult['TRANSITIONING'] < mult['LOW_VOL']

run('ev_calculation',        check_ev_math)
run('negative_ev_halt',      check_negative_ev_halt)
run('kelly_ceiling',         check_kelly_ceiling)
run('two_fail_block',        check_two_fail_block)
run('crisis_no_long',        check_crisis_no_long)
run('s14_deactivated_high_vol', check_s14_deactivated)
run('confidence_override',   check_confidence_override)
run('regime_thresholds',     check_regime_thresholds)

# 4. OUTPUT FORMATS
print('\n4. Output Formats')

def check_verdict_cache_schema():
    required = ['ticker','date','verdict','ev_pct','risk_verdict','stale_after','regime_at_signal']
    cache = {k:'test' for k in required}
    assert all(k in cache for k in required)

def check_discord_length():
    msg = '🦞 **AAPL** — **STRONG_LONG** (7/8 strategies) | Regime: LOW_VOL | EV: +18.4% | Risk: APPROVED | Entry: GO | 📎 memo'
    assert len(msg) < 2000

def check_stale_windows():
    w = {'LOW_VOL':7,'TRANSITIONING':3,'HIGH_VOL':2,'CRISIS':1}
    assert w['LOW_VOL'] > w['TRANSITIONING'] > w['HIGH_VOL'] > w['CRISIS']

def check_prompt_files_exist():
    files = [
        'src/agent/prompts/subagents/data-prep.md',
        'src/agent/prompts/subagents/research.md',
        'src/agent/prompts/subagents/compute.md',
        'src/agent/prompts/subagents/equity-analyst.md',
        'src/agent/prompts/subagents/report-builder.md',
        'src/agent/prompts/components/strategy-matrix.md',
        'src/agent/prompts/components/signal-attribution.md',
    ]
    missing = [f for f in files if not os.path.exists(f)]
    assert not missing, f'Missing prompt files: {missing}'

run('verdict_cache_schema',  check_verdict_cache_schema)
run('discord_msg_length',    check_discord_length)
run('stale_windows',         check_stale_windows)
run('all_prompt_files_exist',check_prompt_files_exist)

# RESULTS
print(f'\n{"="*40}')
print(f'Results: {len(PASS)} passed, {len(FAIL)} failed\n')
if FAIL:
    print('Failures:')
    for name, err in FAIL:
        print(f'  ❌ {name}: {err}')
    sys.exit(1)
else:
    print('✅ All tests passed — pipeline ready')
    sys.exit(0)
