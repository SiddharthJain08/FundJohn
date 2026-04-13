## Strategy × Regime Matrix

Read current regime from `.agents/market-state/latest.json` before any analysis.
Apply this matrix to determine which strategies are active and at what position scale.

````python
import json

with open('.agents/market-state/latest.json') as f:
    regime = json.load(f)

STATE = regime['state']           # LOW_VOL | TRANSITIONING | HIGH_VOL | CRISIS
STRESS = regime['stress_score']   # 0-100
RORO = regime['roro_score']       # -100 to +100
CONFIDENCE = regime['confidence'] # 0-1

POSITION_SCALE = {
    'LOW_VOL': 1.00,
    'TRANSITIONING': 0.55,
    'HIGH_VOL': 0.35,
    'CRISIS': 0.15,
}[STATE]

# If model confidence < 0.60, treat as TRANSITIONING regardless of argmax state
if CONFIDENCE < 0.60 and STATE == 'LOW_VOL':
    STATE = 'TRANSITIONING'
    POSITION_SCALE = 0.55
````

Strategy activation by regime (OFF = do not generate signal):

| ID  | Strategy                  | LOW_VOL | TRANSITIONING | HIGH_VOL | CRISIS |
|-----|---------------------------|---------|---------------|----------|--------|
| S1  | Vol Regime Momentum       | 1.00    | 1.00          | 1.00     | 1.00   |
| S2  | VIX Term Structure Carry  | 1.00    | 0.50          | 1.00     | 1.00   |
| S3  | Credit Spread Warning     | 0.80    | 1.00          | 0.80     | 0.80   |
| S4  | Yield Curve Rotation      | 1.00    | 0.60          | 0.60     | 0.30   |
| S5  | Dollar Regime             | 1.00    | 1.00          | 0.80     | 0.50   |
| S6  | Risk-On/Risk-Off          | 1.00    | 1.00          | 1.00     | 1.00   |
| S7  | Crypto Lead-Lag           | 1.00    | 0.70          | OFF      | OFF    |
| S8  | Commodity Signals         | 1.00    | 1.00          | 1.00     | 0.50   |
| S9  | Dual Momentum             | 1.00    | 0.50          | 0.30     | OFF    |
| S10 | Quality-Value             | 1.00    | 0.80          | 0.80     | 0.30   |
| S11 | Earnings Revisions        | 1.00    | 0.60          | 0.60     | OFF    |
| S12 | Insider Transactions      | 1.00    | 1.00          | 1.00     | 1.00   |
| S13 | Post-Earnings Drift       | 1.00    | 0.60          | 0.30     | OFF    |
| S14 | 52-Week High Momentum     | 1.00    | OFF           | OFF      | OFF    |
| S15 | IV vs RV Arb (sell vol)   | 1.00    | 0.40          | OFF      | OFF    |
| S15 | IV vs RV Arb (buy vol)    | OFF     | 0.80          | 1.00     | OFF    |
| S16 | BSM Mispricing            | 1.00    | 0.60          | 0.40     | OFF    |
| S17 | Dispersion Trading        | 1.00    | 0.50          | 0.80     | OFF    |
| S18 | Put/Call Contrarian       | 1.00    | 0.40          | 1.00     | 1.00   |
| S19 | Pairs Trading             | 1.00    | 0.60          | 0.50     | OFF    |
| S20 | ETF NAV Arb               | 1.00    | 1.00          | 1.00     | 1.00   |

Note: S18 in TRANSITIONING is a regime confirmation signal, NOT a contrarian entry.

Always include at the top of your analysis output:
REGIME_CONTEXT:
state: {STATE}
stress: {STRESS}/100
roro: {RORO}
confidence: {CONFIDENCE:.0%}
position_scale: {POSITION_SCALE:.0%}
active_strategies: {comma-separated list of active IDs}
days_in_state: {regime['days_in_current_state']}
transition_risk: {regime['transition_probs_tomorrow']} chance of state change tomorrow
