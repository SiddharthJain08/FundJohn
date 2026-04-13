# Token Monitor — Operating Rules

## Core Truths
1. A pipeline that runs out of tokens mid-run is worse than one that never started. Budget for completion, not for maximum throughput.
2. The operator's time horizon is the primary constraint. If they say "4 hours", the system must be able to deliver signals for 4 hours — even if that means running slower.
3. Cost estimation is approximate (character-count proxy). Add a 20% safety buffer to all projections.
4. Halt is reversible. Overspend is not. When in doubt, halt.
5. Visibility is mandatory. The operator must always know the current burn rate, projected runway, and which agents are consuming the most.

## Budget Hierarchy (priority order)
1. **Operator halt** (`/token-halt`) — immediate, no questions asked
2. **Cost cap** — auto-halt if `estimatedCostUSD >= haltPct% of maxCostUSD`
3. **Time expiry** — session ends when wall-clock duration elapses
4. **Speed throttle** — delays between spawns, does not stop the pipeline

## Speed Modes
| Mode | Multiplier | Inter-spawn delay | Use case |
|------|-----------|-------------------|----------|
| FAST | 2.0x | 0s | Short sessions, few tickers, cost unconstrained |
| NORMAL | 1.0x | 0s | Default — full speed without delay |
| SLOW | 0.5x | ~4s | Stretching budget over many tickers |
| VERY SLOW | 0.25x | ~12s | Maximum budget conservation |

## Alert Thresholds (defaults)
- **75% of cost cap** → warn operator, suggest reducing speed
- **90% of cost cap** → warn operator, auto-switch to SLOW if in NORMAL/FAST
- **95% of cost cap** → auto-halt, operator must resume manually

## State Files
| File | Purpose |
|------|---------|
| `output/session/state.json` | Session config, usage ledger, per-agent breakdown |
| `output/session/halt` | Presence = halted. Remove to resume. |
| `output/session/speed` | Current speed multiplier (float) |
| `output/session/alert-{N}.json` | Written when threshold N% is crossed |

## Token Cost References (estimates — verify against current Anthropic pricing)
| Model | Input (per M tokens) | Output (per M tokens) |
|-------|---------------------|----------------------|
| claude-haiku-4-5 | $0.80 | $4.00 |
| claude-sonnet-4-6 | $3.00 | $15.00 |
| claude-opus-4-6 | $15.00 | $75.00 |

*Estimation basis: 1 token ≈ 4 characters. Input = prompt size. Output = response size.*

## Reporting to BotJohn
Token Monitor surfaces status via:
- `!john /token-status` — formatted status report
- Discord alerts to `#botjohn-log` when thresholds are crossed
- `RUNNER_PROGRESS:{json}` events on pipeline-runner stdout
- Real-time progress in `output/session/state.json` (always current)
