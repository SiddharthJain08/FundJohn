## Signal Attribution (REQUIRED ON ALL ANALYSIS OUTPUTS)

At the END of every analysis, include a SIGNAL_ATTRIBUTION block.
This block is machine-parsed — format it exactly as shown. Do not omit it.

SIGNAL_ATTRIBUTION:
  verdict: {PROCEED | REVIEW | KILL | NO_SIGNAL}
  active_strategies_checked: {comma-separated IDs}
  signal_metrics:
    - {metric_name}: {one line — why this drove the verdict}
  used_metrics:
    - {metric_name}
  noise_metrics:
    - {metric_name}: {why it was noise}
  strategies_that_agreed: {IDs}
  strategies_that_disagreed: {IDs}
  confluence_score: {N}/{total_active} strategies in agreement
  regime_appropriate: {YES | NO | MARGINAL}
  data_gaps:
    - {description of missing data that would have changed confidence}
  feedback:
    tier_a_coverage: {SUFFICIENT | INSUFFICIENT}
    collection_suggestion: {freeform}
