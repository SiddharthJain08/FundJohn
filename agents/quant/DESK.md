# OpenClaw Quantitative Trading Desk — Rules of Engagement

## Purpose
The trading desk does NOT execute trades. It identifies actionable trade setups, sizes positions, assesses risk, times entries, and generates trade reports for the operator to act on manually.

## Activation Triggers
The desk activates when:
1. A diligence memo with verdict PROCEED is produced
2. The operator runs `!john /trade-scan` to scan all PROCEED names for entry points
3. The operator runs `!john /trade-report` for a daily/weekly portfolio summary
4. A kill signal fires on an existing PROCEED name (triggers exit analysis)

## Risk Limits — Hard Constraints
These are NEVER violated. Every trader agent must check against these before producing output.

- **Max position size**: 10% of stated portfolio value per name
- **Max sector concentration**: 30% of portfolio in any single sector
- **Max total exposure**: 100% net long (no leverage by default)
- **Min position size**: 1% of portfolio (below this, not worth the attention)
- **Stop-loss default**: 15% drawdown from entry triggers mandatory review
- **Max concurrent positions**: 20 names
- **Max correlation**: no more than 3 names with >0.7 pairwise correlation

## Portfolio Context
The desk reads `output/portfolio.json` for current holdings. If this file doesn't exist, assume empty portfolio. The operator updates this file manually.

## Data Freshness
- Price data must be < 30 minutes old for trade signals
- Fundamental data must be < 24 hours old for sizing
- Macro data (rates, VIX) must be < 1 hour old for risk calculations

## Output Destination
All trade reports go to `output/trades/` and are relayed to Discord via BotJohn.

## Pipeline Execution
The desk runs SEQUENTIALLY, not in parallel. Each agent depends on the prior agent's output:
1. Screener → emits trade signals
2. Sizer → sizes each signal
3. Timer → times each sized signal
4. Risk → approves, conditions, or vetoes each fully formed trade
5. Reporter → synthesizes all output into reports

Timeout: 3 minutes per agent, 15 minutes total pipeline.
Model: claude-sonnet-4-6 (all desk agents — requires reasoning, not speed).

## Signal Lifecycle
PENDING_OPERATOR → EXECUTED | REJECTED_BY_OPERATOR | EXPIRED (7 days)
All signals logged to `output/signals.json` — append-only audit trail.
