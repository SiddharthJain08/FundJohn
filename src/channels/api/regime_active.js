// src/channels/api/regime_active.js
// Dashboard "is this strategy active in the current regime?" decision.
//
// This MUST mirror the LIVE engine's gate so the Strategies-page status badge
// (LIVE / WAITING / STALE) agrees with what actually trades. The engine path is
// engine.run_strategies → regime_gate.is_eligible → regime_param_resolver.is_eligible:
//
//     row = get_row(strategy_id, regime_state)   # strategy_regime_params
//     if row is None: return True                # no row → FAIL OPEN
//     return bool(row['eligible'])
//
// Previously the dashboard derived this from code-level `active_in_regimes`, which
// diverged from the DB eligibility the engine enforces (e.g. low_volatility_us was
// eligible=CRISIS only but active_in_regimes=[LOW_VOL,...], so the badge said LIVE
// in a LOW_VOL market while the engine skipped the strategy).

/**
 * @param {Object|null|undefined} regimeParamsForStrategy
 *   Map of { regimeState: eligibleBool } from strategy_regime_params for one
 *   strategy (i.e. regimeParamsById[strategyId]). null/undefined = no rows.
 * @param {string|null|undefined} currentRegime  The current market regime state.
 * @returns {boolean} true iff the live engine would treat the strategy as
 *   eligible-to-trade in `currentRegime`.
 */
function isRegimeEligibleNow(regimeParamsForStrategy, currentRegime) {
  // Defensive: unknown current regime → don't mark inactive on bad input.
  if (!currentRegime) return true;
  const rp = regimeParamsForStrategy;
  // Explicit row for this (strategy, regime) wins; only a strict boolean true is
  // eligible (mirrors bool(row['eligible'])).
  if (rp && Object.prototype.hasOwnProperty.call(rp, currentRegime)) {
    return rp[currentRegime] === true;
  }
  // No row for this regime (whether the map is empty/absent or just missing this
  // key) → engine's resolver returns True (fail open). The dashboard must agree.
  return true;
}

module.exports = { isRegimeEligibleNow };
