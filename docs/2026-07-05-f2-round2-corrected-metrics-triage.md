# F2-round-2 governance triage — CORRECTED metrics (2026-07-05)

Read-only. Registry `approved` cohort (62) scored on the post-re-backtest canonical `strategy_backtest_runs` (true-MTM + always-adverse slippage) against the class-aware promotion floors (equity/etp 0.5 Sharpe / 20% DD; option 0.80/30; crypto 0.50/70). **This is a decision sheet for per-strategy operator sign-off — NOT an auto-deprecation list.**

- **FAIL 45** · PASS 6 · PENDING(re-bt) 4 · INSUFFICIENT(0-trade) 7
- FAIL buckets: negative-Sharpe **32** (clear) · low-positive-Sharpe<floor **9** · DD-only, Sharpe≥floor **4** (judgment — 20% DD floor is tight over a full-history/COVID window)
- Of the 45 FAIL, **40 are currently sized/trading** (total live daily_weight 98.49); the rest are approved-but-unsized.

**Caveats (must read before acting):** (1) DDs are now honestly *higher* than the pre-fix understated values, so the 20% floor bites harder — consider whether the floor should be recalibrated for corrected full-history DD rather than deprecating positive-Sharpe/DD-only names. (2) 4 PENDING rows await the in-flight re-backtest of the 4 stale sized strategies. (3) INSUFFICIENT = 0-trade corrected run (new strategies), not a failure.

| verdict | class | corr Sharpe | corr DD% | floor | sized | live wt | strategy (sid) |
|---|---|--:|--:|---|:-:|--:|---|
| FAIL:sharpe | equity | -5.64 | 16.7 | 0.5/20 | ✅ | 0.50 | low_volatility_us (`low_volatility_us`) |
| FAIL:sharpe | equity | -4.52 | 15.2 | 0.5/20 |  |  | S_constrained_gmv_vcv_dynamics (`S_constrained_gmv_vcv_dynamics`) |
| FAIL:sharpe+dd | equity | -2.42 | 71.2 | 0.5/20 | ✅ |  | S_long_term_price_reversal (`S_long_term_price_reversal`) |
| FAIL:sharpe+dd | equity | -1.94 | 34.4 | 0.5/20 | ✅ | 4.53 | News-Sentiment Long/Short (`S_news_sentiment_long_short`) |
| FAIL:sharpe | equity | -1.70 | 10.9 | 0.5/20 | ✅ | 1.09 | IV/HV Spread Vol Arb (`S21_iv_hv_spread`) |
| FAIL:sharpe+dd | equity | -1.64 | 51.7 | 0.5/20 | ✅ | 1.65 | Options-Flow Confirmed Momentum (`S_options_flow_confirmed_momentum`) |
| FAIL:sharpe+dd | equity | -1.63 | 54.9 | 0.5/20 | ✅ | 2.32 | S_visibility_graph_rsi (`S_visibility_graph_rsi`) |
| FAIL:sharpe+dd | equity | -1.62 | 41.7 | 0.5/20 |  |  | S-HV16 GEX Regime Classifier (`S_HV16_gex_regime`) |
| FAIL:sharpe+dd | equity | -1.53 | 21.0 | 0.5/20 |  |  | IV/RV Arbitrage (`S15_iv_rv_arb`) |
| FAIL:sharpe+dd | equity | -1.40 | 75.3 | 0.5/20 | ✅ | 0.12 | S_nonstationarity_adaptive_selection (`S_nonstationarity_adaptive_selection`) |
| FAIL:sharpe+dd | equity | -1.36 | 28.0 | 0.5/20 | ✅ | 4.20 | S_tr_06_eod_reversal (`S_tr_06_eod_reversal`) |
| FAIL:sharpe+dd | equity | -1.26 | 37.1 | 0.5/20 |  |  | S_HV7_iv_crush_fade (`S_HV7_iv_crush_fade`) |
| FAIL:sharpe+dd | equity | -1.20 | 24.2 | 0.5/20 | ✅ | 4.31 | ReversalMomentumTransitionEarnings (`S_reversal_momentum_transition_earnings`) |
| FAIL:sharpe+dd | equity | -0.96 | 48.5 | 0.5/20 | ✅ | 0.87 | S_price_filter_rule_trend (`S_price_filter_rule_trend`) |
| FAIL:sharpe+dd | equity | -0.87 | 37.5 | 0.5/20 | ✅ | 4.67 | S_prism_vq_cross_section_factor (`S_prism_vq_cross_section_factor`) |
| FAIL:sharpe+dd | etp | -0.74 | 23.1 | 0.5/20 | ✅ | 2.88 | oxf_sma_filter (`oxf_sma_filter`) |
| FAIL:sharpe+dd | equity | -0.71 | 25.9 | 0.5/20 | ✅ | 3.11 | S_intl_momentum_attention_regime (`S_intl_momentum_attention_regime`) |
| FAIL:sharpe+dd | etp | -0.66 | 23.3 | 0.5/20 | ✅ | 2.06 | oxf_smash_day_b (`oxf_smash_day_b`) |
| FAIL:sharpe+dd | equity | -0.65 | 48.1 | 0.5/20 |  |  | S15_insider_opportunistic_short (`S15_insider_opportunistic_short`) |
| FAIL:sharpe | equity | -0.61 | 12.5 | 0.5/20 | ✅ | 0.79 | S-HV20 IV Dispersion Reversion (`S_HV20_iv_dispersion_reversion`) |
| FAIL:sharpe+dd | equity | -0.59 | 28.1 | 0.5/20 | ✅ | 2.73 | S_ma_tsmom_crossover (`S_ma_tsmom_crossover`) |
| FAIL:sharpe+dd | equity | -0.51 | 35.6 | 0.5/20 | ✅ | 2.27 | S_barbell_trend_horizon (`S_barbell_trend_horizon`) |
| FAIL:sharpe+dd | equity | -0.42 | 32.5 | 0.5/20 | ✅ | 0.17 | S24_52wk_high_proximity (`S24_52wk_high_proximity`) |
| FAIL:sharpe+dd | etp | -0.39 | 20.0 | 0.5/20 | ✅ | 1.74 | oxf_vortex (`oxf_vortex`) |
| FAIL:sharpe+dd | crypto | -0.32 | 90.9 | 0.5/70 | ✅ | 2.16 | S_btc_momentum (`S_btc_momentum`) |
| FAIL:sharpe | equity | -0.28 | 12.1 | 0.5/20 | ✅ | 2.68 | S_HV8_gamma_theta_carry (`S_HV8_gamma_theta_carry`) |
| FAIL:sharpe | equity | -0.19 | 15.6 | 0.5/20 | ✅ | 4.42 | S_epistemic_rank_gate (`S_epistemic_rank_gate`) |
| FAIL:sharpe+dd | equity | -0.15 | 25.1 | 0.5/20 | ✅ |  | Insider Cluster Buy (`S12_insider`) |
| FAIL:sharpe+dd | equity | -0.13 | 78.4 | 0.5/20 | ✅ | 0.55 | S23_regime_momentum (`S23_regime_momentum`) |
| FAIL:sharpe | equity | -0.06 | 14.0 | 0.5/20 | ✅ | 6.44 | S_price_earnings_momentum_drift (`S_price_earnings_momentum_drift`) |
| FAIL:sharpe+dd | equity | -0.05 | 66.6 | 0.5/20 | ✅ | 1.65 | Dual Momentum (`S9_dual_momentum`) |
| FAIL:sharpe+dd | equity | -0.01 | 55.5 | 0.5/20 | ✅ | 2.28 | JT 12-Month Momentum (Top 5) (`S_custom_jt_momentum_12mo`) |
| FAIL:sharpe+dd | equity | 0.04 | 48.2 | 0.5/20 | ✅ | 3.55 | Dual Momentum (12-1mo, Top7) (`S25_dual_momentum`) |
| FAIL:sharpe | etp | 0.12 | 16.4 | 0.5/20 | ✅ | 0.38 | oxf_rsi2_meanrev (`oxf_rsi2_meanrev`) |
| FAIL:sharpe | etp | 0.16 | 16.3 | 0.5/20 | ✅ | 2.40 | S_ast_asset_class_trend_following (`S_ast_asset_class_trend_following`) |
| FAIL:sharpe | equity | 0.22 | 17.8 | 0.5/20 | ✅ | 2.25 | S_idiosyncratic_vol_puzzle (`S_idiosyncratic_vol_puzzle`) |
| FAIL:sharpe | equity | 0.34 | 14.3 | 0.5/20 | ✅ | 2.58 | S_markov_frontier_regimes (`S_markov_frontier_regimes`) |
| FAIL:sharpe+dd | equity | 0.37 | 39.1 | 0.5/20 | ✅ | 4.34 | S22QualityMomentum (`S22_quality_momentum`) |
| FAIL:sharpe+dd | equity | 0.39 | 29.8 | 0.5/20 | ✅ | 3.53 | S_sparse_basis_pursuit_sdf (`S_sparse_basis_pursuit_sdf`) |
| FAIL:sharpe+dd | equity | 0.40 | 31.0 | 0.5/20 | ✅ | 3.01 | S_macro_risk_momentum_ip_beta (`S_macro_risk_momentum_ip_beta`) |
| FAIL:sharpe | equity | 0.50 | 13.1 | 0.5/20 | ✅ | 4.77 | S_cross_sectional_price_momentum (`S_cross_sectional_price_momentum`) |
| FAIL:dd | etp | 0.54 | 26.8 | 0.5/20 | ✅ | 1.18 | S_commodity_etp_momentum (`S_commodity_etp_momentum`) |
| FAIL:dd | equity | 0.58 | 21.0 | 0.5/20 | ✅ | 4.93 | S_extreme_intraday_reversal_nasdaq (`S_extreme_intraday_reversal_nasdaq`) |
| FAIL:dd | equity | 0.70 | 34.4 | 0.5/20 | ✅ | 2.68 | S25_dual_momentum_v2 (`S25_dual_momentum_v2`) |
| FAIL:dd | equity | 1.72 | 24.7 | 0.5/20 | ✅ | 2.71 | momentum_12_1 (`momentum_12_1`) |
| PENDING | equity | -0.08 | 26.4 | 0.5/20 | ✅ | 1.83 | S_pairs_trading_jump_diffusion_intraday (`S_pairs_trading_jump_diffusion_intraday`) |
| PENDING | equity | 0.36 | 27.2 | 0.5/20 | ✅ | 2.12 | S_tr_03_bocpd_change_point (`S_tr_03_bocpd_change_point`) |
| PENDING | equity | 0.37 | 7.1 | 0.5/20 | ✅ | 2.27 | S_tr_02_hurst_regime_flip (`S_tr_02_hurst_regime_flip`) |
| PENDING | equity | 0.76 | 14.5 | 0.5/20 | ✅ | 3.13 | S_ivol_mispricing_asymmetry (`S_ivol_mispricing_asymmetry`) |
| INSUFFICIENT | equity | — | 0.0 | 0.5/20 |  |  | S_tr_01_vvix_early_warning (`S_tr_01_vvix_early_warning`) |
| INSUFFICIENT | equity | — | 0.0 | 0.5/20 |  |  | S_tr_04_intraday_spy_momentum (`S_tr_04_intraday_spy_momentum`) |
| INSUFFICIENT | equity | — | 0.0 | 0.5/20 |  |  | S_bankruptcy_risk_anomaly (`S_bankruptcy_risk_anomaly`) |
| INSUFFICIENT | equity | — | 0.0 | 0.5/20 |  |  | Quality Value (`S10_quality_value`) |
| INSUFFICIENT | equity | — | 0.0 | 0.5/20 |  |  | S_skewness_dispersion_macro (`S_skewness_dispersion_macro`) |
| INSUFFICIENT | equity | — | 0.0 | 0.5/20 |  |  | S-HV17 Earnings Straddle Fade (`S_HV17_earnings_straddle_fade`) |
| INSUFFICIENT | equity | — | 0.0 | 0.5/20 |  |  | S_local_global_balance (`S_local_global_balance`) |
| PASS | equity | 0.60 | 18.3 | 0.5/20 | ✅ | 5.75 | PTreePanelTangency (`S_ptree_panel_tangency`) |
| PASS | equity | 0.79 | 14.8 | 0.5/20 | ✅ | 7.06 | S_value_momentum_everywhere (`S_value_momentum_everywhere`) |
| PASS | equity | 0.91 | 13.5 | 0.5/20 | ✅ | 2.13 | S-HV19 Vega-Weighted IV Surface Tilt (`S_HV19_iv_surface_tilt`) |
| PASS | equity | 0.93 | 17.7 | 0.5/20 | ✅ | 6.34 | S_fama_french_anomaly_dissection (`S_fama_french_anomaly_dissection`) |
| PASS | equity | 2.02 | 5.1 | 0.5/20 |  |  | S_labor_day_week_momentum_reversal (`S_labor_day_week_momentum_reversal`) |
| PASS | equity | 2.38 | 3.7 | 0.5/20 |  |  | S_fomc_presell_spy_long (`S_fomc_presell_spy_long`) |
