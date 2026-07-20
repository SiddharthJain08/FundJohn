"""
Strategy Registry — loads and validates all approved strategy implementations.
The execution engine calls get_approved_strategies() to get the active set.
"""

import importlib
import os
import sys
import logging
from typing import Dict, List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)

# Map strategy DB id → Python class
# Canonical IDs (no numeric prefix)
_IMPL_MAP = {
    'max_pain':          ('strategies.implementations.s5_max_pain',          'MaxPainGravity'),
    'dual_momentum':     ('strategies.implementations.s09_dual_momentum',     'DualMomentum'),
    'quality_value':     ('strategies.implementations.s10_quality_value',     'QualityValue'),
    'insider_cluster_buy': ('strategies.implementations.s12_insider', 'InsiderClusterBuy'),
    'iv_rv_arb':         ('strategies.implementations.s15_iv_rv_arb',         'IVRVArb'),
    'jt_momentum_12mo':  ('strategies.implementations.S_custom_jt_momentum_12mo', 'JTMomentum12Mo'),
    'S_momentum_delayed_overreaction': ('strategies.implementations.s_momentum_delayed_overreaction', 'MomentumDelayedOverreaction'),
    # S-HV7  S-HV12: hardcoded HV strategies (zero LLM tokens)
    'S_HV7_iv_crush_fade':           ('strategies.implementations.shv7_iv_crush_fade',            'IVCrushFade'),
    'S_HV8_gamma_theta_carry':       ('strategies.implementations.shv8_gamma_theta_carry',        'GammaThetaCarry'),
    'S_HV9_rv_momentum_div':         ('strategies.implementations.shv9_rv_momentum_div',          'RVMomentumDivergence'),
    'S_HV10_triple_gate_fear':       ('strategies.implementations.shv10_triple_gate_fear',        'TripleGateFear'),
    'S_HV11_cross_stock_dispersion': ('strategies.implementations.shv11_cross_stock_dispersion',  'CrossStockDispersion'),
    'S_HV12_vrp_normalization':      ('strategies.implementations.shv12_vrp_normalization',       'VRPNormalization'),
    # Legacy aliases — keeps existing DB signal records valid
    'S5_max_pain':       ('strategies.implementations.s5_max_pain',          'MaxPainGravity'),
    'S9_dual_momentum':  ('strategies.implementations.s09_dual_momentum',     'DualMomentum'),
    'S10_quality_value': ('strategies.implementations.s10_quality_value',     'QualityValue'),
    'S12_insider':       ('strategies.implementations.s12_insider', 'InsiderClusterBuy'),
    'S15_insider_opportunistic_short': ('strategies.implementations.s15_insider_opportunistic_short', 'OpportunisticInsiderShort'),
    'S15_iv_rv_arb':     ('strategies.implementations.s15_iv_rv_arb',         'IVRVArb'),
    'S_custom_jt_momentum_12mo': ('strategies.implementations.S_custom_jt_momentum_12mo', 'JTMomentum12Mo'),
    'S23_regime_momentum':      ('strategies.implementations.S23_regime_momentum',      'RegimeMomentumStrategy'),
    'S24_52wk_high_proximity':  ('strategies.implementations.S24_52wk_high_proximity',  'FiftyTwoWeekHighProximityStrategy'),
    'S25_dual_momentum':        ('strategies.implementations.S25_dual_momentum',         'DualMomentum'),
    'S25_dual_momentum_v2':     ('strategies.implementations.S25_dual_momentum',         'DualMomentum'),
    # Cross-sector corroboration (2026-05-29). ① passed the equity gate (Sharpe 0.96 / MaxDD
    # 17.3% on 2024-04-22..2026-04-22; base-only -0.47 → strong PCR-corroboration lift).
    # ② sector-flow NOT registered (failed gate, all modes negative).
    # ③ news-sentiment PASSED on real backfilled news (Sharpe 0.996 / MaxDD 11.8% / +49.6% over
    # 2022-11-15..2026-04-30, 29439 trades; positive in every regime). NEWS-ONLY (live adds social).
    'S_options_flow_confirmed_momentum': ('strategies.implementations.S_options_flow_confirmed_momentum', 'OptionsFlowConfirmedMomentum'),
    'S_news_sentiment_long_short': ('strategies.implementations.S_news_sentiment_long_short', 'NewsSentimentLongShort'),
        'S_HV13_call_put_iv_spread': ('strategies.implementations.shv13_call_put_iv_spread', 'CallPutIVSpread'),
        'S_HV14_otm_skew_factor':      ('strategies.implementations.shv14_otm_skew_factor',      'OTMSkewFactor'),
        'S_HV15_iv_term_structure':    ('strategies.implementations.shv15_iv_term_structure',    'IVTermStructure'),
        'S_HV16_gex_regime':           ('strategies.implementations.shv16_gex_regime',           'GEXRegime'),
        'S_HV17_earnings_straddle_fade': ('strategies.implementations.shv17_earnings_straddle_fade', 'EarningsStraddleFade'),
        'S_HV19_iv_surface_tilt':      ('strategies.implementations.shv19_iv_surface_tilt',      'IVSurfaceTilt'),
        'S_HV20_iv_dispersion_reversion': ('strategies.implementations.shv20_iv_dispersion_reversion', 'IVDispersionReversion'),
    'S1_event_driven_new_news':       ('strategies.implementations.s1_event_driven_new_news',       'EventDrivenNewNews'),
    'S_markov_frontier_regimes':      ('strategies.implementations.S_markov_frontier_regimes',      'MarkovFrontierRegimes'),
    'S_regime_specialist_vol':        ('strategies.implementations.S_regime_specialist_vol',        'RegimeSpecialistVol'),
    'S_epistemic_rank_gate':          ('strategies.implementations.S_epistemic_rank_gate',          'EpistemicRankGate'),
    'S_price_path_convexity':         ('strategies.implementations.S_price_path_convexity',         'PricePathConvexity'),
    'S_robust_minimum_variance_hedge': ('strategies.implementations.S_robust_minimum_variance_hedge', 'RobustMinimumVarianceHedge'),
    'S_skewness_dispersion_macro':     ('strategies.implementations.S_skewness_dispersion_macro',     'SkewnessDispersionMacro'),
    'S_quantum_rebalance_qaoa':        ('strategies.implementations.S_quantum_rebalance_qaoa',        'QuantumRebalanceQAOA'),
    'S_alpha191_lasso_crossmarket':    ('strategies.implementations.S_alpha191_lasso_crossmarket',    'Alpha191LassoCrossMarket'),
    'S_barbell_trend_horizon':         ('strategies.implementations.S_barbell_trend_horizon',         'BarbellTrendHorizon'),
    'S_nonstationarity_adaptive_selection': ('strategies.implementations.S_nonstationarity_adaptive_selection', 'NonStationarityAdaptiveSelection'),
    'S_local_global_balance':              ('strategies.implementations.S_local_global_balance',              'LocalGlobalBalance'),
    # Transitioning-regime specialists (Phases 1–4)
    'S_tr_01_vvix_early_warning':          ('strategies.implementations.S_tr_01_vvix_early_warning',          'VVIXEarlyWarning'),
    'S_tr_02_hurst_regime_flip':           ('strategies.implementations.S_tr_02_hurst_regime_flip',           'HurstRegimeFlip'),
    'S_tr_03_bocpd_change_point':          ('strategies.implementations.S_tr_03_bocpd_change_point',          'BOCPDChangePoint'),
    'S_tr_04_intraday_spy_momentum':       ('strategies.implementations.S_tr_04_intraday_spy_momentum',       'IntradaySPYMomentum'),
    'S_tr_06_eod_reversal':                ('strategies.implementations.S_tr_06_eod_reversal',                'EODCrossSectionalReversal'),
    'S21_iv_hv_spread':                   ('strategies.implementations.S21_iv_hv_spread',                    'IVHVSpread'),
    'S_sparse_basis_pursuit_sdf':         ('strategies.implementations.S_sparse_basis_pursuit_sdf',          'SparseBasisPursuitSdf'),
    # ── Top-10 cohort (2026-04) — shimmering-skipping-engelbart plan-ID ──
    'S_HV13_call_put_iv_spread_cohort2026':   ('strategies.implementations.shv13_call_put_iv_spread_cohort2026',   'CallPutIVSpread'),
    'S_HV14_otm_skew_factor_cohort2026':      ('strategies.implementations.shv14_otm_skew_factor_cohort2026',      'OTMSkewFactor'),
    'S_HV15_iv_term_structure_cohort2026':    ('strategies.implementations.shv15_iv_term_structure_cohort2026',    'IVTermStructureSlope'),
    'S_HV17_earnings_straddle_fade_cohort2026': ('strategies.implementations.shv17_earnings_straddle_fade_cohort2026', 'EarningsStraddleFade'),
    'S_HV20_iv_dispersion_reversion_cohort2026': ('strategies.implementations.shv20_iv_dispersion_reversion_cohort2026', 'IVDispersionReversion'),
    'S_TR01_vvix_early_warning':              ('strategies.implementations.str01_vvix_early_warning',              'VVIXEarlyWarning'),
    'S_TR02_hurst_regime_flip':               ('strategies.implementations.str02_hurst_regime_flip',               'HurstRegimeFlip'),
    'S_TR03_bocpd':                           ('strategies.implementations.str03_bocpd',                           'BOCPDDetector'),
    'S_TR04_zarattini_intraday_spy':          ('strategies.implementations.str04_zarattini_intraday_spy',          'ZarattiniIntradaySPY'),
    'S_TR06_baltussen_eod_reversal':          ('strategies.implementations.str06_baltussen_eod_reversal',          'BaltussenEODReversal'),
    'low_volatility_us':                      ('strategies.implementations.low_volatility_us',                      'LowVolatilityUS'),
    'S_cross_sectional_price_momentum':       ('strategies.implementations.S_cross_sectional_price_momentum',       'CrossSectionalPriceMomentum'),
    'S_pairs_trading_jump_diffusion_intraday': ('strategies.implementations.S_pairs_trading_jump_diffusion_intraday', 'PairsTradingJumpDiffusionIntraday'),
    'S_ivol_mispricing_asymmetry': ('strategies.implementations.s_ivol_mispricing_asymmetry', 'IvolMispricingAsymmetry'),
    'S_conditional_coskewness_factor': ('strategies.implementations.S_conditional_coskewness_factor', 'ConditionalCoskewnessFactor'),
    'S_daily_high_ml_classifier':      ('strategies.implementations.S_daily_high_ml_classifier',      'DailyHighMLClassifier'),
    'S_extreme_intraday_reversal_nasdaq': ('strategies.implementations.S_extreme_intraday_reversal_nasdaq', 'ExtremeIntradayReversalNasdaq'),
    'S_volume_return_autocorr_lmsw': ('strategies.implementations.S_volume_return_autocorr_lmsw', 'VolumeReturnAutocorrLMSW'),
    'S_long_term_price_reversal':    ('strategies.implementations.S_long_term_price_reversal',    'LongTermPriceReversal'),
    'S_fama_french_five_factor':     ('strategies.implementations.S_fama_french_five_factor',     'FamaFrenchFiveFactor'),
    'S_renko_kagi_pairs_spread':     ('strategies.implementations.S_renko_kagi_pairs_spread',     'RenkoKagiPairsSpread'),
    'S_expected_idiosyncratic_skewness': ('strategies.implementations.S_expected_idiosyncratic_skewness', 'ExpectedIdiosyncraticSkewness'),
    'S_partial_cointegration_pairs': ('strategies.implementations.S_partial_cointegration_pairs', 'PartialCointegrationPairs'),
    'S_pairs_trading_min_distance':  ('strategies.implementations.S_pairs_trading_min_distance',  'PairsTradingMinDistance'),
    'S_least_squares_risk_parity':   ('strategies.implementations.S_least_squares_risk_parity',    'LeastSquaresRiskParity'),
    'S_sparse_cca_mean_revert':      ('strategies.implementations.S_sparse_cca_mean_revert',         'SparseCCAMeanRevert'),
    'S_institutional_lead_lag':      ('strategies.implementations.S_institutional_lead_lag',          'InstitutionalLeadLag'),
    'S_pairs_cointegration_copula':  ('strategies.implementations.S_pairs_cointegration_copula',  'PairsCointegrationCopula'),
    'S_ma_tsmom_crossover':          ('strategies.implementations.S_ma_tsmom_crossover',          'MATSMOMCrossover'),
    'S_pca_etf_stat_arb_reversion':  ('strategies.implementations.S_pca_etf_stat_arb_reversion',  'PCAETFStatArbReversion'),
    'S_price_earnings_momentum_drift': ('strategies.implementations.s_price_earnings_momentum_drift', 'PriceEarningsMomentumDrift'),
    'S_3d_pca_characteristic_factors': ('strategies.implementations.S_3d_pca_characteristic_factors', 'ThreeDPCACharacteristicFactors'),
    'S_bankruptcy_risk_anomaly': ('strategies.implementations.S_bankruptcy_risk_anomaly', 'BankruptcyRiskAnomaly'),
    'S_quality_adjusted_size': ('strategies.implementations.S_quality_adjusted_size', 'QualityAdjustedSize'),
    'S_risk_neutral_skew_cross_section': ('strategies.implementations.S_risk_neutral_skew_cross_section', 'RiskNeutralSkewCrossSection'),
    'S22_quality_momentum': ('strategies.implementations.S22_quality_momentum', 'S22QualityMomentum'),
    'S_fear_index_return_prediction': ('strategies.implementations.S_fear_index_return_prediction', 'FearIndexReturnPrediction'),
    'S_vix_conditioned_short_reversal': ('strategies.implementations.S_vix_conditioned_short_reversal', 'VixConditionedShortReversal'),
    'S_q_theory_investment_growth':     ('strategies.implementations.S_q_theory_investment_growth',     'QTheoryInvestmentGrowth'),
    'S_macro_risk_momentum_ip_beta':    ('strategies.implementations.S_macro_risk_momentum_ip_beta',    'MacroRiskMomentumIPBeta'),
    'S_path_dependent_vol_pdv':         ('strategies.implementations.S_path_dependent_vol_pdv',         'PathDependentVolPDV'),
    'S_industry_momentum_moskowitz':    ('strategies.implementations.S_industry_momentum_moskowitz',    'IndustryMomentumMoskowitz'),
    'S_vp_macd_index_sensitivity':      ('strategies.implementations.S_vp_macd_index_sensitivity',      'VPMACDIndexSensitivity'),
    'S_crisp_signal_aware_hrp':         ('strategies.implementations.S_crisp_signal_aware_hrp',         'CrispSignalAwareHRP'),
    'momentum_12_1':                    ('strategies.implementations.momentum_12_1',                    'Momentum12_1'),
    'S_intl_momentum_attention_regime': ('strategies.implementations.S_intl_momentum_attention_regime', 'IntlMomentumAttentionRegime'),
    'S_constrained_gmv_vcv_dynamics':   ('strategies.implementations.S_constrained_gmv_vcv_dynamics',   'ConstrainedGMVVCVDynamics'),
    'S_tda_topological_risk_portfolio': ('strategies.implementations.S_tda_topological_risk_portfolio', 'TdaTopologicalRiskPortfolio'),
    'S_ptree_panel_tangency':           ('strategies.implementations.S_ptree_panel_tangency',           'PTreePanelTangency'),
    'S_thgnn_correlation_basket':       ('strategies.implementations.s_thgnn_correlation_basket',       'THGNNCorrelationBasket'),
    'S_reversal_momentum_transition_earnings': ('strategies.implementations.S_reversal_momentum_transition_earnings', 'ReversalMomentumTransitionEarnings'),
    'S_transitioning_overbought_revert': ('strategies.implementations.S_transitioning_overbought_revert', 'TransitioningOverboughtRevert'),
    'S_ap_tree_cross_section_sdf': ('strategies.implementations.S_ap_tree_cross_section_sdf', 'APTreeCrossSectionSDF'),
    'S_abnormal_accruals_earnings_quality': ('strategies.implementations.S_abnormal_accruals_earnings_quality', 'AbnormalAccrualsEarningsQuality'),
    'S_motif_orbit_spillover_portfolio': ('strategies.implementations.S_motif_orbit_spillover_portfolio', 'MotifOrbitSpilloverPortfolio'),
    'S_btc_gold_dual_momentum_rotation': ('strategies.implementations.S_btc_gold_dual_momentum_rotation', 'BtcGoldDualMomentumRotation'),
    'S_p_index_eef_weekly_rotation': ('strategies.implementations.S_p_index_eef_weekly_rotation', 'PIndexEEFWeeklyRotation'),
    'S_empirical_bayes_shrinkage_mv': ('strategies.implementations.S_empirical_bayes_shrinkage_mv', 'EmpiricalBayesShrinkageMV'),
    'S_mvgarch_nig_crra_portfolio':   ('strategies.implementations.S_mvgarch_nig_crra_portfolio',   'MvGarchNigCrraPortfolio'),
    'S_bayes_stein_shrinkage_mvo':    ('strategies.implementations.S_bayes_stein_shrinkage_mvo',    'BayesSteinShrinkageMVO'),
    'S_visibility_graph_rsi':         ('strategies.implementations.S_visibility_graph_rsi',         'VisibilityGraphRSI'),
    'S_prism_vq_cross_section_factor': ('strategies.implementations.S_prism_vq_cross_section_factor', 'PrismVQCrossSectionFactor'),
    # Tier-B staging — awaiting data backfill; stubs return [] until gate clears
    'S_estimation_risk_three_fund':    ('strategies.implementations.s_estimation_risk_three_fund',    'S_estimation_risk_three_fund'),
    'S_earnings_news_specific_momentum': ('strategies.implementations.s_earnings_news_specific_momentum', 'S_earnings_news_specific_momentum'),
    'S_price_filter_rule_trend': ('strategies.implementations.S_price_filter_rule_trend', 'PriceFilterRuleTrend'),
    'S_ivol_cross_section_quintile': ('strategies.implementations.S_ivol_cross_section_quintile', 'IvolCrossSectionQuintile'),
    'S_fomc_presell_spy_long': ('strategies.implementations.S_fomc_presell_spy_long', 'FomcPresellSpyLong'),
    'S_labor_day_week_momentum_reversal': ('strategies.implementations.S_labor_day_week_momentum_reversal', 'LaborDayWeekMomentumReversal'),
    'S_financial_constraint_kz_factor': ('strategies.implementations.S_financial_constraint_kz_factor', 'FinancialConstraintKZFactor'),
    'S_january_btm_size_seasonal': ('strategies.implementations.S_january_btm_size_seasonal', 'JanuaryBTMSizeSeasonal'),
    'S_fama_french_three_factor_composite': ('strategies.implementations.S_fama_french_three_factor_composite', 'FamaFrenchThreeFactorComposite'),
    'S_spx_death_cross_contrarian_fade': ('strategies.implementations.S_spx_death_cross_contrarian_fade', 'SpxDeathCrossContrarianFade'),
    'S_growth_inflation_sector_timing':  ('strategies.implementations.S_growth_inflation_sector_timing',  'GrowthInflationSectorTiming'),
    # SP-3: commodity ETP momentum — reference strategy for instrument_class='etp' rails
    'S_commodity_etp_momentum':          ('strategies.implementations.S_commodity_etp_momentum',          'CommodityEtpMomentum'),
    # SP-3.1: BTC momentum — reference strategy for instrument_class='crypto' rails
    'S_btc_momentum':                    ('strategies.implementations.S_btc_momentum',                    'BtcMomentum'),
    # SP-4 Phase 0: short straddle VRP — reference strategy for instrument_class='option' rails
    'S_short_straddle_vrp':              ('strategies.implementations.S_short_straddle_vrp',              'ShortStraddleVRP'),
    # Cotton (2026) — Schur-damped MV/HRP shrinkage portfolio
    'S_schur_damped_minvar_shrinkage':   ('strategies.implementations.S_schur_damped_minvar_shrinkage',   'SchurDampedMinVarShrinkage'),
    # Value + Momentum Everywhere — Asness, Moskowitz & Pedersen (2013)
    'S_value_momentum_everywhere':       ('strategies.implementations.S_value_momentum_everywhere',       'ValueMomentumEverywhere'),
    # Ang et al. 2006 — idiosyncratic volatility puzzle
    'S_idiosyncratic_vol_puzzle':        ('strategies.implementations.s_idiosyncratic_vol_puzzle',        'IdiosyncraticVolPuzzle'),
    # DellaVigna & Pollet 2009: Friday earnings inattention drift (PEAD premium)
    'S_friday_earnings_inattention_drift': ('strategies.implementations.S_friday_earnings_inattention_drift', 'FridayEarningsInattentionDrift'),
    # Daniel & Titman 1997: characteristics cross-section (size + B/M long-short)
    'S_daniel_titman_characteristics_cross_section': ('strategies.implementations.S_daniel_titman_characteristics_cross_section', 'DanielTitmanCharacteristicsCrossSection'),
    # Hirshleifer, Lim & Teoh 2009: distraction hypothesis PEAD
    'S_earnings_distraction_pead': ('strategies.implementations.S_earnings_distraction_pead', 'EarningsDistractionPEAD'),
    # Fama & French 2008: NSI + accruals + momentum composite cross-sectional anomaly
    'S_fama_french_anomaly_dissection': ('strategies.implementations.S_fama_french_anomaly_dissection', 'FamaFrenchAnomalyDissection'),
    # Fama & French 1996: three-factor anomalies (HML + SMB composite long-short)
    'S_fama_french_three_factor_anomalies': ('strategies.implementations.S_fama_french_three_factor_anomalies', 'FamaFrenchThreeFactorAnomalies'),
    # Davis, Fama & French 2000: book-to-market value premium (B/M long-short, R3000)
    'S_book_to_market_value_premium': ('strategies.implementations.S_book_to_market_value_premium', 'BookToMarketValuePremium'),
    # Herculano 2026: Bayesian Parametric Portfolio Policy (L1-shrunk PPP coefficients)
    'S_bppp_bayesian_parametric_weights': ('strategies.implementations.S_bppp_bayesian_parametric_weights', 'BPPPBayesianParametricWeights'),
    # Meb Faber Tactical Yield: term/credit premium expanding-window percentile → IEF/LQD/cash
    'S_tactical_yield_credit_term_premium': ('strategies.implementations.S_tactical_yield_credit_term_premium', 'TacticalYieldCreditTermPremium'),
    # Tier-B staging — awaiting data backfill; stubs return [] until gate clears
    'S_bm_divy_market_timing':              ('strategies.implementations.s_bm_divy_market_timing',              'S_bm_divy_market_timing'),
    'S_rd_intensity_intangible_factor':     ('strategies.implementations.s_rd_intensity_intangible_factor',     'S_rd_intensity_intangible_factor'),
    'S_growth_defensive_smooth_score_timing': ('strategies.implementations.s_growth_defensive_smooth_score_timing', 'S_growth_defensive_smooth_score_timing'),
    # Chen, Tang, Yao & Zhou 2021: PLS composite investor attention index times SPY
    'S_investor_attention_market_timing': ('strategies.implementations.S_investor_attention_market_timing', 'InvestorAttentionMarketTiming'),
    # Frazzini & Pedersen 2014: Betting Against Beta — long low-beta decile, short high-beta decile
    'S_ast_betting_against_beta_factor_in_stocks': ('strategies.implementations.S_ast_betting_against_beta_factor_in_stocks', 'BettingAgainstBetaFactorInStocks'),
    # Quantpedia: Asset Class Trend Following — 210-day SMA filter across 5-ETF basket
    'S_ast_asset_class_trend_following': ('strategies.implementations.S_ast_asset_class_trend_following', 'AssetClassTrendFollowing'),
    # Quantpedia: Value Factor CAPE Effect Within Countries — annual CAPE rotation across 25 country ETFs
    'S_ast_value_factor_effect_within_countries': ('strategies.implementations.S_ast_value_factor_effect_within_countries', 'AstValueFactorEffectWithinCountries'),
    # Asness, Moskowitz & Pedersen 2013: 12-1 momentum rotation across 11 multi-asset ETFs
    'S_ast_value_and_momentum_factors_across_asset_classes': ('strategies.implementations.S_ast_value_and_momentum_factors_across_asset_classes', 'AstValueAndMomentumFactorsAcrossAssetClasses'),
    # QuantPedia: Turn of the Month — long SPY last day of month, exit 3rd day of new month
    'S_ast_turn_of_the_month_in_equity_indexes': ('strategies.implementations.S_ast_turn_of_the_month_in_equity_indexes', 'AstTurnOfTheMonthInEquityIndexes'),
    # Quantpedia: Trend Following Effect in Stocks — ATH-breakout + ATR(10) trailing stop
    'S_ast_trend_following_effect_in_stocks': ('strategies.implementations.S_ast_trend_following_effect_in_stocks', 'AstTrendFollowingEffectInStocks'),
    # Quantpedia: Short-Term Reversal in Stocks — weekly long/short cross-sectional reversal
    'S_ast_short_term_reversal_in_stocks': ('strategies.implementations.S_ast_short_term_reversal_in_stocks', 'AstShortTermReversalInStocks'),
    # Quantpedia: Momentum and Reversal combined with Volatility — long/short intersection of top-quintile 6M return + top-quintile realized vol
    'S_ast_momentum_and_reversal_combined_with_volatility_effect_in_stocks': ('strategies.implementations.S_ast_momentum_and_reversal_combined_with_volatility_effect_in_stocks', 'AstMomentumAndReversalCombinedWithVolatilityEffectInStocks'),
    # Quantpedia: Sector Momentum Rotational System — monthly top-3 252-day ROC rotation across 10 sector ETFs
    'S_ast_sector_momentum_rotational_system': ('strategies.implementations.S_ast_sector_momentum_rotational_system', 'SectorMomentumRotationalSystem'),
    # Oxford/blueprint batch — impl files + manifest entries existed but _IMPL_MAP
    # wiring was never done, so all 30 were unimportable (12 live ones were
    # "dead live": manifest live, 0 registry rows, 0 signals ever). Wired
    # 2026-06-19 per operator. Live ones still need registry approval + weights
    # to actually trade; candidates stay candidate but are now backtestable.
    'oxf_adaptive_ma':            ('strategies.implementations.oxf_adaptive_ma',            'OxfAdaptiveMa'),
    'oxf_aroon_breakout':         ('strategies.implementations.oxf_aroon_breakout',         'OxfAroonBreakout'),
    'oxf_bollinger_momentum':     ('strategies.implementations.oxf_bollinger_momentum',     'OxfBollingerMomentum'),
    'oxf_bull_oops':              ('strategies.implementations.oxf_bull_oops',              'OxfBullOops'),
    'oxf_donchian_breakout':      ('strategies.implementations.oxf_donchian_breakout',      'OxfDonchianBreakout'),
    'oxf_dow_theory':             ('strategies.implementations.oxf_dow_theory',             'OxfDowTheory'),
    'oxf_dual_momentum_roc':      ('strategies.implementations.oxf_dual_momentum_roc',      'OxfDualMomentumRoc'),
    'oxf_false_breakout':         ('strategies.implementations.oxf_false_breakout',         'OxfFalseBreakout'),
    'oxf_frama':                  ('strategies.implementations.oxf_frama',                  'OxfFrama'),
    'oxf_gap_a':                  ('strategies.implementations.oxf_gap_a',                  'OxfGapA'),
    'oxf_greatest_swing_value':   ('strategies.implementations.oxf_greatest_swing_value',   'OxfGreatestSwingValue'),
    'oxf_heikin_ashi':            ('strategies.implementations.oxf_heikin_ashi',            'OxfHeikinAshi'),
    'oxf_hook':                   ('strategies.implementations.oxf_hook',                   'OxfHook'),
    'oxf_hull_ma':                ('strategies.implementations.oxf_hull_ma',                'OxfHullMa'),
    'oxf_keltner':                ('strategies.implementations.oxf_keltner',                'OxfKeltner'),
    'oxf_linreg_slope':           ('strategies.implementations.oxf_linreg_slope',           'OxfLinregSlope'),
    'oxf_livermore':              ('strategies.implementations.oxf_livermore',              'OxfLivermore'),
    'oxf_macd_zero':              ('strategies.implementations.oxf_macd_zero',              'OxfMacdZero'),
    'oxf_nr7':                    ('strategies.implementations.oxf_nr7',                    'OxfNR7'),
    'oxf_orbp_momentum':          ('strategies.implementations.oxf_orbp_momentum',          'OxfOrbpMomentum'),
    'oxf_price_momentum':         ('strategies.implementations.oxf_price_momentum',         'OxfPriceMomentum'),
    'oxf_ross_hook':              ('strategies.implementations.oxf_ross_hook',              'OxfRossHook'),
    'oxf_rsi2_meanrev':           ('strategies.implementations.oxf_rsi2_meanrev',           'OxfRsi2Meanrev'),
    'oxf_sma_filter':             ('strategies.implementations.oxf_sma_filter',             'OxfSmaFilter'),
    'oxf_smash_day_b':            ('strategies.implementations.oxf_smash_day_b',            'OxfSmashDayB'),
    'oxf_td_sequential':          ('strategies.implementations.oxf_td_sequential',          'OxfTdSequential'),
    'oxf_vortex':                 ('strategies.implementations.oxf_vortex',                 'OxfVortex'),
    'oxf_welles_wilder_breakout': ('strategies.implementations.oxf_welles_wilder_breakout', 'OxfWellesWilderBreakout'),
    'oxf_wyckoff_meanrev':        ('strategies.implementations.oxf_wyckoff_meanrev',        'OxfWyckoffMeanrev'),
    'oxf_zero_lag_ma':            ('strategies.implementations.oxf_zero_lag_ma',            'OxfZeroLagMa'),
    # Quantpedia: WTI-Brent spread mean reversion via USO/BNO ETF proxies
    'S_wti_brent_spread_mean_reversion': ('strategies.implementations.S_wti_brent_spread_mean_reversion', 'WtiBrentSpreadMeanReversion'),
    # Moskowitz, Ooi & Pedersen (2012): Time Series Momentum across 24 multi-asset ETFs
    'S_ast_time_series_momentum_effect': ('strategies.implementations.S_ast_time_series_momentum_effect', 'AstTimeSeriesMomentumEffect'),
    # Quantpedia: Skewness Effect in Commodities — monthly quintile sort on 252-day skewness across commodity ETFs
    'S_ast_skewness_effect_in_commodities': ('strategies.implementations.S_ast_skewness_effect_in_commodities', 'AstSkewnessEffectInCommodities'),
    # Longmore 2026: Triangulated stat arb — OLS spread across stock triplets, z-score mean reversion
    'S_triangulated_stat_arb_triplets': ('strategies.implementations.S_triangulated_stat_arb_triplets', 'TriangulatedStatArbTriplets'),
    # Safari & Schmidhuber 2026: quadratic trend-strength model forecasts next-day variance → vol-targeted long-short
    'S_trend_vol_quadratic_forecast': ('strategies.implementations.S_trend_vol_quadratic_forecast', 'TrendVolQuadraticForecast'),
    # Quantpedia: Return Asymmetry Effect in Commodity Futures — monthly long/short IE-ranked ETP proxies
    'S_ast_return_asymmetry_effect_in_commodity_futures': ('strategies.implementations.S_ast_return_asymmetry_effect_in_commodity_futures', 'ReturnAsymmetryEffectInCommodityFutures'),
    # Quantpedia: Rebalancing Premium in Cryptocurrencies — daily equal-weight rebalancing BTC-USD + ETH-USD
    'S_ast_rebalancing_premium_in_cryptocurrencies': ('strategies.implementations.S_ast_rebalancing_premium_in_cryptocurrencies', 'RebalancingPremiumInCryptocurrencies'),
    # Quantpedia: Payday Anomaly — long SPY on monthly payday (15th or prior Friday), exit next day
    'S_ast_payday_anomaly': ('strategies.implementations.S_ast_payday_anomaly', 'AstPaydayAnomaly'),
    # Quantpedia: Pairs Trading with Country ETFs — mean-reversion long/short across 23 MSCI country ETFs + SPY
    'S_ast_pairs_trading_with_country_etfs': ('strategies.implementations.S_ast_pairs_trading_with_country_etfs', 'AstPairsTradingWithCountryEtfs'),
    # Quantpedia: Paired Switching — quarterly rotation between SPY and AGG based on 90-day trailing return
    'S_ast_paired_switching': ('strategies.implementations.S_ast_paired_switching', 'AstPairedSwitching'),
    # Zhao 2026: gradient-boosting insider purchase signals in microcap equities ($30M–$500M)
    'S_microcap_insider_purchase_momentum': ('strategies.implementations.S_microcap_insider_purchase_momentum', 'MicrocapInsiderPurchaseMomentum'),
    # Devanathan et al. 2026: proportional-control vol targeting on IVV/SPY ETP
    'S_adaptive_vol_control_proportional': ('strategies.implementations.S_adaptive_vol_control_proportional', 'AdaptiveVolControlProportional'),
    # Roman 2026: SVD condition-number MRI over 11 sector ETFs — monthly SPY timing gate
    'S_market_rank_indicator_timing': ('strategies.implementations.S_market_rank_indicator_timing', 'MarketRankIndicatorTiming'),
    # Quantpedia: Option Expiration Week Effect — long OEF Mon–Thu of 3rd-Friday expiry week
    'S_ast_option_expiration_week_effect': ('strategies.implementations.S_ast_option_expiration_week_effect', 'OptionExpirationWeekEffect'),
    # Quantpedia: Momentum in Mutual Fund Returns — quarterly top-decile 6-month momentum rotation across broad ETF proxy basket
    'S_ast_momentum_in_mutual_fund_returns': ('strategies.implementations.S_ast_momentum_in_mutual_fund_returns', 'MomentumInMutualFundReturns'),
    # Quantpedia: Momentum Factor Effect in Stocks — monthly long top-quintile / short bottom-quintile UMD portfolio (Jegadeesh & Titman)
    'S_ast_momentum_factor_effect_in_stocks': ('strategies.implementations.S_ast_momentum_factor_effect_in_stocks', 'AstMomentumFactorEffectInStocks'),
    # Quantpedia: Momentum Factor and Style Rotation Effect — monthly long winner / short loser across 6 Russell/S&P style ETFs
    'S_ast_momentum_factor_and_style_rotation_effect': ('strategies.implementations.S_ast_momentum_factor_and_style_rotation_effect', 'MomentumFactorStyleRotation'),
    # Quantpedia: Momentum Effect in Commodities — monthly long/short quintile on 252-day ROC across commodity ETF proxies
    'S_ast_momentum_effect_in_commodities': ('strategies.implementations.S_ast_momentum_effect_in_commodities', 'AstMomentumEffectInCommodities'),
    # Quantpedia: Market Sentiment and Overnight Anomaly — SPY overnight hold scaled by SPY/VIX SMA conditions
    'S_ast_market_sentiment_and_an_overnight_anomaly': ('strategies.implementations.S_ast_market_sentiment_and_an_overnight_anomaly', 'MarketSentimentOvernightAnomaly'),
    # Singha, Aguilera-Toste & Lahiri 2025: forecast-to-fill EMA trend + momentum z-score on GLD, vol-targeted Kelly
    'S_gold_trend_momentum_vol_target': ('strategies.implementations.S_gold_trend_momentum_vol_target', 'GoldTrendMomentumVolTarget'),
    # Quantpedia: Low Volatility Factor Effect in Stocks — monthly long bottom-quartile (lowest 3yr weekly vol) large-cap equities
    'S_ast_low_volatility_factor_effect_in_stocks': ('strategies.implementations.S_ast_low_volatility_factor_effect_in_stocks', 'AstLowVolatilityFactorEffectInStocks'),
    # Aarab 2025: Aligned Economic Index market timing under yield-curve state-switching
    'S_aligned_economic_index_regime_timing': ('strategies.implementations.S_aligned_economic_index_regime_timing', 'AlignedEconomicIndexRegimeTiming'),
    # Quantpedia: Asset Growth Effect — annual June long/short decile sort on YoY total-asset growth
    'S_ast_asset_growth_effect': ('strategies.implementations.S_ast_asset_growth_effect', 'AssetGrowthEffect'),
    # Quantpedia: ROA Effect Within Stocks — monthly long/short decile sort by trailing ROA within large/small-cap halves
    'S_ast_roa_effect_within_stocks': ('strategies.implementations.S_ast_roa_effect_within_stocks', 'AstROAEffectWithinStocks'),
    # Quantpedia: Accrual Anomaly (Sloan 1996) — annual May long/short decile on balance-sheet accruals
    'S_accrual_anomaly': ('strategies.implementations.S_accrual_anomaly', 'AccrualAnomaly'),
    # Quantpedia: Combining F-Score and Short-Term Reversals — monthly long past losers (F≥7) / short past winners (F≤3)
    'S_ast_combining_fundamental_fscore_and_equity_short_term_reversals': ('strategies.implementations.S_ast_combining_fundamental_fscore_and_equity_short_term_reversals', 'AstCombiningFscoreShortTermReversals'),
    # Quantpedia: Earnings Announcement Premium — long high-VCR expected announcers / short non-announcers, monthly
    'S_ast_earnings_announcement_premium': ('strategies.implementations.S_ast_earnings_announcement_premium', 'EarningsAnnouncementPremium'),
    # Quantpedia: Earnings Quality Factor — composite quality score long/short, annual rebalance end of June
    'S_ast_earnings_quality_factor': ('strategies.implementations.S_ast_earnings_quality_factor', 'EarningsQualityFactor'),
    # Quantpedia: Earnings Announcements + Buybacks — long upcoming-earnings stocks with prior insider-buy signal
    'S_ast_earnings_announcements_combined_with_stock_repurchases': ('strategies.implementations.S_ast_earnings_announcements_combined_with_stock_repurchases', 'AstEarningsAnnouncementsCombinedWithStockRepurchases'),
    # Quantpedia: Value Factor Effect Within Countries — annual December rotation into cheapest-tercile country ETFs by Shiller CAPE
    'S_ast_value_factor_effect_within_countries': ('strategies.implementations.S_ast_value_factor_effect_within_countries', 'ValueFactorEffectWithinCountries'),
    # Quantpedia: FED Model — yield-gap OLS predictor for SPY vs SHY rotation
    'S_ast_fed_model':                                               ('strategies.implementations.S_ast_fed_model',                                               'AstFedModel'),
    # Quantpedia: Momentum Factor Combined with Asset Growth Effect — monthly long/short within highest-asset-growth decile
    'S_ast_momentum_factor_combined_with_asset_growth_effect': ('strategies.implementations.S_ast_momentum_factor_combined_with_asset_growth_effect', 'MomentumFactorCombinedWithAssetGrowthEffect'),
    # Quantpedia: R&D Expenditures and Stock Returns — annual April long/short quintile on 5yr decay-weighted R&D-to-MarketCap
    'S_ast_rd_expenditures_and_stock_returns': ('strategies.implementations.S_ast_rd_expenditures_and_stock_returns', 'RDExpendituresAndStockReturns'),
    # Quantpedia: Residual Momentum Factor — OLS-orthogonalized 12-1M score long/short decile (Blitz, Huij & Martens 2011)
    'S_ast_residual_momentum_factor': ('strategies.implementations.S_ast_residual_momentum_factor', 'AstResidualMomentumFactor'),
    # Quantpedia: Value Book-to-Market Factor — long bottom-quintile P/B (value), short top-quintile P/B (growth), annual December rebalance
    'S_ast_value_book_to_market_factor': ('strategies.implementations.S_ast_value_book_to_market_factor', 'AstValueBookToMarketFactor'),
    # Chen, Hansen & Tong 2026: Split-Session Cluster GARCH — overnight/intraday EWMA blend + sector clustering → long-only GMV
    'S_split_session_cluster_garch_gmv': ('strategies.implementations.S_split_session_cluster_garch_gmv', 'SplitSessionClusterGarchGMV'),
    # Ansari, Jain & Iyer 2026: Marchenko-Pastur spectral denoising → Markov-chain coreness → LONG top-40 peripheral assets
    'S_spectral_denoising_peripheral_portfolio': ('strategies.implementations.S_spectral_denoising_peripheral_portfolio', 'SpectralDenoisingPeripheralPortfolio'),
    # Basilico 2026: dash-for-cash calendar overlay — long/short momentum quintiles in last 6 trading days of month
    'S_intramonth_momentum_cycle': ('strategies.implementations.S_intramonth_momentum_cycle', 'IntramontMomentumCycle'),
    # LeCompte, Suominen & Hjalmarsson 2026: TSMOM gated by Boundaries metric (CAPE + div-yield + term-spread extremes)
    'S_valuation_bounded_tsmom': ('strategies.implementations.S_valuation_bounded_tsmom', 'S_valuation_bounded_tsmom'),
}


def load_strategy_class(strategy_id: str):
    """Import and return the class for a given strategy_id. Returns None on failure."""
    if strategy_id not in _IMPL_MAP:
        logger.warning(f"No implementation registered for strategy_id={strategy_id}")
        return None

    module_path, class_name = _IMPL_MAP[strategy_id]
    try:
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        return cls
    except Exception as e:
        logger.error(f"Failed to load {strategy_id}: {e}")
        return None


def get_approved_strategies(db_rows: List[dict]) -> List:
    """
    Given a list of strategy_registry rows from Postgres (dicts), return
    instantiated strategy objects for those that are approved and have an
    implementation registered.

    db_rows expected fields: id, parameters (dict), status
    """
    instances = []
    for row in db_rows:
        sid    = row.get('id')
        status = row.get('status', '')
        if status != 'approved':
            continue

        cls = load_strategy_class(sid)
        if cls is None:
            continue

        params = row.get('parameters') or {}
        try:
            instance = cls(parameters=params)
            # Override id with the DB row id so signals are written with the correct FK key
            instance.id = sid
            instances.append(instance)
            logger.info(f"Loaded strategy: {sid}")
        except Exception as e:
            logger.error(f"Failed to instantiate {sid}: {e}")

    return instances


def list_registered_ids() -> List[str]:
    """Return all strategy IDs with registered implementations."""
    return list(_IMPL_MAP.keys())


def validate_all() -> Dict[str, bool]:
    """Try importing all registered implementations. Returns {id: ok}."""
    results = {}
    for sid in _IMPL_MAP:
        cls = load_strategy_class(sid)
        results[sid] = cls is not None
    return results

def list_all_strategy_ids() -> list:
    """Return all registered strategy IDs."""
    return list(_IMPL_MAP.keys())
