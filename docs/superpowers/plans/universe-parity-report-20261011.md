# Universe parity epoch — before/after (epoch start 2026-10-07 UTC)

bench vector (activation comparator): CRISIS=1.575, HIGH_VOL=0.533, LOW_VOL=0.938, TRANSITIONING=0.470; excess=0.0
live strategies: 106; with both a pre and post run: 103; no post run yet: 1

| strategy | univ pre→post | source/tier | Sharpe pre→post (Δ) | trades pre→post | elig pre → post | implication |
|---|---|---|---|---|---|---|
| S_HV20_iv_dispersion_reversion | 12884→4047 | filter_ref/tier_liquid | -0.39→-6.74 (-6.34) | 13→77 | ∅ → ∅ | ineligible in every regime before and after |
| S_options_flow_confirmed_momentum | 12884→1038 | filter_ref/tier_r1000 | -4.36→-0.84 (+3.52) | 117→1170 | ∅ → LOW_VOL | would be ACTIVATED in LOW_VOL |
| S_HV19_iv_surface_tilt | 12884→4047 | filter_ref/tier_liquid | -9.10→-5.68 (+3.43) | 52→116 | ∅ → ∅ | ineligible in every regime before and after |
| low_volatility_us | 12883→2691 | filter_ref/tier_r3000 | -2.47→-0.15 (+2.32) | 32759→91717 | ∅ → ∅ | ineligible in every regime before and after |
| S21_iv_hv_spread | 12884→4048 | filter_ref/tier_liquid | -2.64→-1.15 (+1.49) | 414→586 | ∅ → ∅ | ineligible in every regime before and after |
| S_HV8_gamma_theta_carry | 12884→4047 | filter_ref/tier_liquid | -1.81→-3.25 (-1.44) | 1356→2119 | ∅ → ∅ | ineligible in every regime before and after |
| S_fama_french_anomaly_dissection | 12884→3374 | filter_ref/stocks_liquid | 0.07→-0.94 (-1.01) | 18506→53007 | ∅ → ∅ | ineligible in every regime before and after; Sharpe turned non-positive (fails the class gate) |
| S_extreme_intraday_reversal_nasdaq | 12884→450 | filter_ref/sp500 | 0.99→0.01 (-0.98) | 36314→49025 | HIGH_VOL → HIGH_VOL,CRISIS | would be ACTIVATED in CRISIS |
| S_value_momentum_everywhere | 12885→3375 | filter_ref/stocks_liquid | 0.11→-0.74 (-0.85) | 19283→53804 | ∅ → ∅ | ineligible in every regime before and after; Sharpe turned non-positive (fails the class gate) |
| S_ptree_panel_tangency | 12884→3081 | filter_ref/stocks_liquid | 0.19→-0.58 (-0.77) | 43217→91541 | ∅ → ∅ | ineligible in every regime before and after; Sharpe turned non-positive (fails the class gate) |
| S_ma_tsmom_crossover | 12884→449 | filter_ref/sp500 | -0.35→0.42 (+0.77) | 77936→117295 | ∅ → ∅ | ineligible in every regime before and after |
| S_downside_beta_premium | 12884→1040 | filter_ref/tier_r1000 | -0.11→-0.87 (-0.76) | 1370→1635 | TRANSITIONING → ∅ | would be DEACTIVATED in TRANSITIONING by the next activation_assigner --all; DORMANT after (eligible nowhere) — falls out of the sizer |
| S_long_term_price_reversal | 12884→465 | filter_ref/sp500 | -0.86→-0.14 (+0.72) | 45074→93389 | ∅ → ∅ | ineligible in every regime before and after |
| S_macro_risk_momentum_ip_beta | 12884→3226 | filter_ref/stocks_liquid | 0.20→-0.45 (-0.65) | 19842→58531 | CRISIS → ∅ | would be DEACTIVATED in CRISIS by the next activation_assigner --all; DORMANT after (eligible nowhere) — falls out of the sizer; Sharpe turned non-positive (fails the class gate) |
| S_visibility_graph_rsi | 12884→444 | filter_ref/sp500 | -1.43→-0.86 (+0.57) | 78120→124822 | ∅ → ∅ | ineligible in every regime before and after |
| S_overnight_intraday_tug_of_war | 12884→1038 | filter_ref/tier_r1000 | 0.09→0.65 (+0.56) | 1293→1574 | LOW_VOL → LOW_VOL,HIGH_VOL | would be ACTIVATED in HIGH_VOL; live eligibility differs from post-epoch verdict in HIGH_VOL |
| S_price_earnings_momentum_drift | 12884→2821 | filter_ref/tier_r3000 | -0.51→0.04 (+0.54) | 67846→93705 | ∅ → ∅ | ineligible in every regime before and after |
| S_ast_roa_effect_within_stocks | 12883→3027 | filter_ref/stocks_liquid | 0.05→0.56 (+0.51) | 17975→12644 | ∅ → LOW_VOL,TRANSITIONING | would be ACTIVATED in LOW_VOL,TRANSITIONING |
| S_nonstationarity_adaptive_selection | 12884→1038 | filter_ref/tier_r1000 | -0.21→-0.71 (-0.50) | 40425→112207 | ∅ → ∅ | ineligible in every regime before and after |
| S_ast_residual_momentum_factor | 12883→2897 | filter_ref/tier_r3000 | -0.42→0.05 (+0.47) | 2201→3834 | ∅ → HIGH_VOL | would be ACTIVATED in HIGH_VOL |
| S_triangulated_stat_arb_triplets | 12884→3154 | filter_ref/stocks_liquid | -0.62→-0.17 (+0.45) | 8893→18157 | CRISIS → CRISIS | no activation change |
| S_ast_asset_class_trend_following | 12883→4106 | filter_ref/tier_liquid | 0.53→0.09 (-0.44) | 390→320 | TRANSITIONING → TRANSITIONING | no activation change |
| S_volume_shock_overnight_drift | 12886→2693 | filter_ref/tier_r3000 | 0.26→0.69 (+0.43) | 4409→3715 | CRISIS → HIGH_VOL | would be DEACTIVATED in CRISIS by the next activation_assigner --all; would be ACTIVATED in HIGH_VOL; live eligibility differs from post-epoch verdict in CRISIS |
| S_microcap_insider_purchase_momentum | 12884→2746 | filter_ref/tier_r3000 | -0.08→0.30 (+0.38) | 13725→11794 | ∅ → ∅ | ineligible in every regime before and after |
| S_ast_trend_following_effect_in_stocks | 12883→1040 | filter_ref/tier_r1000 | -0.21→0.16 (+0.37) | 45450→63873 | ∅ → ∅ | ineligible in every regime before and after |
| S_amihud_illiquidity_premium | 4051→4057 | cap/tier_liquid | -0.60→-0.25 (+0.35) | 1532→1550 | ∅ → ∅ | ineligible in every regime before and after |
| oxf_td_sequential | 12883→4047 | filter_ref/tier_liquid | -0.25→0.08 (+0.33) | 2631→2598 | ∅ → ∅ | ineligible in every regime before and after |
| momentum_12_1 | 12883→3087 | filter_ref/stocks_liquid | 0.30→0.62 (+0.32) | 12637→20448 | CRISIS → CRISIS | no activation change |
| S24_52wk_high_proximity | 12884→3083 | filter_ref/stocks_liquid | -0.16→0.15 (+0.30) | 2415→4662 | ∅ → CRISIS | would be ACTIVATED in CRISIS; live eligibility differs from post-epoch verdict in CRISIS |
| S_sparse_basis_pursuit_sdf | 12884→1042 | filter_ref/tier_r1000 | 0.01→0.30 (+0.29) | 11992→20487 | ∅ → LOW_VOL,HIGH_VOL | would be ACTIVATED in LOW_VOL,HIGH_VOL |
| S_same_month_seasonality | 12884→2948 | filter_ref/tier_r3000 | 0.10→-0.17 (-0.27) | 755→818 | ∅ → ∅ | ineligible in every regime before and after; Sharpe turned non-positive (fails the class gate) |
| S_markov_frontier_regimes | 12884→4228 | filter_ref/tier_liquid | 0.84→0.58 (-0.26) | 13950→19428 | HIGH_VOL,CRISIS → HIGH_VOL,CRISIS | no activation change |
| S_investor_attention_market_timing | 12884→4254 | filter_ref/tier_liquid | 0.48→0.72 (+0.24) | 1497→1530 | LOW_VOL → LOW_VOL,HIGH_VOL | would be ACTIVATED in HIGH_VOL |
| S_earnings_sue_pead | 12883→3027 | filter_ref/stocks_liquid | 2.94→3.16 (+0.21) | 7388→8013 | LOW_VOL,TRANSITIONING,HIGH_VOL → LOW_VOL,TRANSITIONING,HIGH_VOL,CRISIS | would be ACTIVATED in CRISIS |
| S_ast_time_series_momentum_effect | 12883→4123 | filter_ref/tier_liquid | 0.76→0.55 (-0.21) | 1087→1187 | TRANSITIONING,HIGH_VOL → HIGH_VOL | would be DEACTIVATED in TRANSITIONING by the next activation_assigner --all; live eligibility differs from post-epoch verdict in TRANSITIONING |
| S_ast_pairs_trading_with_country_etfs | 12883→4069 | filter_ref/tier_liquid | -0.29→-0.49 (-0.20) | 9752→9147 | HIGH_VOL → ∅ | would be DEACTIVATED in HIGH_VOL by the next activation_assigner --all; DORMANT after (eligible nowhere) — falls out of the sizer; live eligibility differs from post-epoch verdict in HIGH_VOL |
| S_tr_06_eod_reversal | 12885→2691 | filter_ref/tier_r3000 | -1.21→-1.02 (+0.19) | 78968→112686 | ∅ → ∅ | ineligible in every regime before and after |
| S_cross_sectional_price_momentum | 12883→1042 | filter_ref/tier_r1000 | -0.12→0.07 (+0.19) | 37581→94995 | ∅ → ∅ | ineligible in every regime before and after |
| S25_dual_momentum_v2 | 12884→4132 | filter_ref/tier_liquid | -0.36→-0.18 (+0.18) | 8704→14290 | ∅ → ∅ | ineligible in every regime before and after |
| oxf_nr7 | 12883→4047 | filter_ref/tier_liquid | -0.93→-0.76 (+0.17) | 12866→12592 | ∅ → ∅ | ineligible in every regime before and after |
| oxf_orbp_momentum | 12883→4047 | filter_ref/tier_liquid | -0.62→-0.45 (+0.16) | 49355→48152 | ∅ → ∅ | ineligible in every regime before and after |
| S25_dual_momentum | 12885→4132 | filter_ref/tier_liquid | 0.08→0.24 (+0.16) | 8640→14290 | ∅ → ∅ | ineligible in every regime before and after |
| S_reversal_momentum_transition_earnings | 12884→2821 | filter_ref/tier_r3000 | -0.33→-0.48 (-0.15) | 73743→96340 | ∅ → ∅ | ineligible in every regime before and after |
| S23_regime_momentum | 12884→3029 | filter_ref/stocks_liquid | -1.01→-1.16 (-0.15) | 1961→3986 | ∅ → ∅ | ineligible in every regime before and after |
| S_insider_drawdown_confirmation | 12884→2748 | filter_ref/tier_r3000 | 0.81→0.67 (-0.14) | 2336→2266 | TRANSITIONING → ∅ | would be DEACTIVATED in TRANSITIONING by the next activation_assigner --all; DORMANT after (eligible nowhere) — falls out of the sizer |
| oxf_vortex | 12883→4069 | filter_ref/tier_liquid | -0.11→-0.24 (-0.12) | 3198→3197 | ∅ → ∅ | ineligible in every regime before and after |
| S_intl_momentum_attention_regime | 12884→3153 | filter_ref/stocks_liquid | -0.34→-0.46 (-0.12) | 14244→22823 | CRISIS → ∅ | would be DEACTIVATED in CRISIS by the next activation_assigner --all; DORMANT after (eligible nowhere) — falls out of the sizer |
| S_news_sentiment_long_short | 12884→3027 | filter_ref/stocks_liquid | -1.39→-1.27 (+0.11) | 36192→39402 | ∅ → ∅ | ineligible in every regime before and after |
| oxf_dual_momentum_roc | 12883→2704 | filter_ref/tier_r3000 | -0.34→-0.23 (+0.11) | 58302→56097 | ∅ → ∅ | ineligible in every regime before and after |
| S_window_dress_loser_reversal | 12896→12900 | none/— | 0.39→0.28 (-0.11) | 2212→2109 | HIGH_VOL → HIGH_VOL | no activation change |
| oxf_price_momentum | 12883→2704 | filter_ref/tier_r3000 | -0.35→-0.24 (+0.11) | 58286→56866 | ∅ → ∅ | ineligible in every regime before and after |
| S_leadlag_cluster_minirocket_v2 | 12884→12900 | none/— | 0.07→-0.03 (-0.11) | 4764→4824 | CRISIS → CRISIS | Sharpe turned non-positive (fails the class gate) |
| S_gold_trend_momentum_vol_target | 12884→4228 | filter_ref/tier_liquid | 1.37→1.26 (-0.10) | 866→875 | LOW_VOL,TRANSITIONING,HIGH_VOL → LOW_VOL,TRANSITIONING,HIGH_VOL | no activation change |
| oxf_livermore | 12883→4047 | filter_ref/tier_liquid | -0.60→-0.50 (+0.09) | 44549→43669 | ∅ → ∅ | ineligible in every regime before and after |
| S_ast_earnings_announcements_combined_with_stock_repurchases | 12884→3027 | filter_ref/stocks_liquid | -0.37→-0.46 (-0.09) | 42301→45946 | ∅ → ∅ | ineligible in every regime before and after |
| oxf_smash_day_b | 12883→4047 | filter_ref/tier_liquid | -0.51→-0.41 (+0.09) | 8537→8542 | ∅ → ∅ | ineligible in every regime before and after |
| S_idiosyncratic_vol_puzzle | 12884→1042 | filter_ref/tier_r1000 | -0.09→-0.00 (+0.09) | 24792→72630 | ∅ → ∅ | ineligible in every regime before and after |
| oxf_dow_theory | 12883→4047 | filter_ref/tier_liquid | -0.44→-0.35 (+0.09) | 41361→39708 | ∅ → ∅ | ineligible in every regime before and after |
| S_price_filter_rule_trend | 12884→12900 | none/— | -0.42→-0.35 (+0.08) | 23854→24094 | ∅ → ∅ | ineligible in every regime before and after |
| S_tr_03_bocpd_change_point | 4066→4073 | cap/tier_liquid | -1.81→-1.73 (+0.07) | 112459→112282 | ∅ → ∅ | ineligible in every regime before and after |
| oxf_ross_hook | 12883→4047 | filter_ref/tier_liquid | 0.14→0.07 (-0.07) | 5207→5015 | HIGH_VOL → HIGH_VOL | no activation change |
| S_regime_age_momentum | 12884→3049 | filter_ref/stocks_liquid | 0.66→0.59 (-0.06) | 1038→1194 | ∅ → ∅ | ineligible in every regime before and after |
| oxf_sma_filter | 12883→4130 | filter_ref/tier_liquid | 0.03→0.08 (+0.06) | 56234→54704 | ∅ → ∅ | ineligible in every regime before and after |
| oxf_rsi2_meanrev | 12883→4106 | filter_ref/tier_liquid | 0.56→0.50 (-0.05) | 2116→2090 | TRANSITIONING,HIGH_VOL → TRANSITIONING,HIGH_VOL | no activation change |
| S_barbell_trend_horizon | 12883→444 | filter_ref/sp500 | -0.40→-0.45 (-0.05) | 37765→118976 | ∅ → ∅ | ineligible in every regime before and after |
| S_ast_earnings_announcement_premium | 12884→3044 | filter_ref/stocks_liquid | -0.15→-0.20 (-0.05) | 1103→1977 | HIGH_VOL → CRISIS | would be DEACTIVATED in HIGH_VOL by the next activation_assigner --all; would be ACTIVATED in CRISIS |
| S_sparse_cca_mean_revert | 1040→1038 | cap/tier_r1000 | -0.20→-0.25 (-0.04) | 10087→10238 | ∅ → ∅ | ineligible in every regime before and after |
| S_ast_turn_of_the_month_in_equity_indexes | 12883→4047 | filter_ref/tier_liquid | 1.32→1.36 (+0.04) | 372→375 | TRANSITIONING → TRANSITIONING | no activation change |
| S_ast_sector_momentum_rotational_system | 12883→4123 | filter_ref/tier_liquid | 0.45→0.49 (+0.04) | 348→351 | ∅ → ∅ | ineligible in every regime before and after |
| S_conformal_kelly_etp_sizing | 12883→12900 | none/— | 0.76→0.72 (-0.04) | 8810→8936 | CRISIS → CRISIS | no activation change |
| S_custom_jt_momentum_12mo | 4125→4132 | cap/tier_liquid | 0.54→0.57 (+0.04) | 10543→10396 | CRISIS → CRISIS | no activation change |
| oxf_bollinger_momentum | 12883→4061 | filter_ref/tier_liquid | -0.47→-0.44 (+0.04) | 13435→12778 | ∅ → ∅ | ineligible in every regime before and after |
| S_two_day_pullback_atr_uptrend | 12884→12900 | none/— | 0.36→0.39 (+0.03) | 547→557 | LOW_VOL → ∅ | would be DEACTIVATED in LOW_VOL by the next activation_assigner --all; DORMANT after (eligible nowhere) — falls out of the sizer; live eligibility differs from post-epoch verdict in LOW_VOL |
| S_tsmom_cross_asset_vol_scaled | 12885→4229 | filter_ref/tier_liquid | -0.22→-0.18 (+0.03) | 39223→39546 | CRISIS → CRISIS | no activation change |
| S_prism_vq_cross_section_factor | 12884→3081 | filter_ref/stocks_liquid | -0.47→-0.44 (+0.03) | 12894→20373 | ∅ → ∅ | ineligible in every regime before and after |
| S_epistemic_rank_gate | 12883→1038 | filter_ref/tier_r1000 | -0.15→-0.18 (-0.03) | 74676→111725 | HIGH_VOL → ∅ | would be DEACTIVATED in HIGH_VOL by the next activation_assigner --all; DORMANT after (eligible nowhere) — falls out of the sizer |
| S_vp_macd_index_sensitivity | 4117→4124 | cap/tier_liquid | 0.82→0.84 (+0.02) | 884→892 | LOW_VOL,HIGH_VOL → LOW_VOL,HIGH_VOL | no activation change |
| S_ast_option_expiration_week_effect | 12883→4047 | filter_ref/tier_liquid | -0.02→-0.00 (+0.02) | 488→492 | LOW_VOL → LOW_VOL | no activation change |
| S_ast_value_and_momentum_factors_across_asset_classes | 12883→4132 | filter_ref/tier_liquid | 0.44→0.46 (+0.02) | 454→458 | ∅ → ∅ | ineligible in every regime before and after |
| oxf_welles_wilder_breakout | 12883→4047 | filter_ref/tier_liquid | -0.37→-0.35 (+0.02) | 36262→35954 | ∅ → ∅ | ineligible in every regime before and after |
| S22_quality_momentum | 12885→3087 | filter_ref/stocks_liquid | 0.08→0.06 (-0.02) | 10553→16642 | CRISIS → CRISIS | no activation change |
| S_wti_brent_spread_mean_reversion | 12884→4048 | filter_ref/tier_liquid | -0.02→-0.04 (-0.02) | 2619→2638 | ∅ → ∅ | ineligible in every regime before and after |
| S_crypto_time_series_momentum | 12896→12900 | none/— | 0.24→0.22 (-0.02) | 432→432 | ∅ → ∅ | ineligible in every regime before and after |
| S_tr_02_hurst_regime_flip | 4118→4124 | cap/tier_liquid | -1.18→-1.16 (+0.02) | 99851→97830 | ∅ → ∅ | ineligible in every regime before and after |
| S_btc_kelly_powerlaw_vol_decay | 12892→12900 | none/— | -0.96→-0.95 (+0.01) | 702→709 | ∅ → ∅ | ineligible in every regime before and after |
| oxf_donchian_breakout | 12883→4048 | filter_ref/tier_liquid | -0.15→-0.16 (-0.01) | 10956→11066 | ∅ → ∅ | ineligible in every regime before and after |
| S9_dual_momentum | 12884→4124 | filter_ref/tier_liquid | 0.58→0.59 (+0.01) | 5331→8614 | LOW_VOL,CRISIS → LOW_VOL,CRISIS | no activation change |
| S_kalman_laguerre_trend_crossover | 4042→4048 | cap/tier_liquid | 0.23→0.22 (-0.01) | 1578→1603 | HIGH_VOL → HIGH_VOL | no activation change |
| S_evar_tempered_stable_sector_etf | 12884→12900 | none/— | 0.75→0.76 (+0.01) | 13340→13395 | HIGH_VOL,CRISIS → HIGH_VOL,CRISIS | no activation change |
| S_ast_market_sentiment_and_an_overnight_anomaly | 12883→4047 | filter_ref/tier_liquid | 1.13→1.12 (-0.01) | 1944→1952 | LOW_VOL,TRANSITIONING,HIGH_VOL → LOW_VOL,TRANSITIONING,HIGH_VOL | no activation change |
| S_beta_spy | 1→1 | cap/tier_liquid | 0.66→0.67 (+0.01) | 2618→2636 | LOW_VOL,CRISIS → LOW_VOL,TRANSITIONING,CRISIS | would be ACTIVATED in TRANSITIONING; live eligibility differs from post-epoch verdict in HIGH_VOL |
| S_ast_fed_model | 12883→4123 | filter_ref/tier_liquid | 0.66→0.67 (+0.01) | 2330→2348 | HIGH_VOL → HIGH_VOL | no activation change |
| S_sector_trend_overlay | 12884→12900 | none/— | 0.57→0.57 (+0.01) | 3564→3585 | ∅ → ∅ | ineligible in every regime before and after |
| S_commodity_etp_momentum | 12883→4057 | filter_ref/tier_liquid | 0.81→0.80 (-0.01) | 1450→1450 | TRANSITIONING,HIGH_VOL → TRANSITIONING,HIGH_VOL | no activation change |
| S_intraday_first_half_hour_momentum_etf | 12884→4047 | filter_ref/tier_liquid | 0.95→0.96 (+0.01) | 2931→2951 | LOW_VOL,HIGH_VOL,CRISIS → LOW_VOL,HIGH_VOL,CRISIS | no activation change |
| S_vol_indicators_sp500_drawdown_timing | 12884→4233 | filter_ref/tier_liquid | 0.84→0.83 (-0.00) | 1674→1693 | LOW_VOL,HIGH_VOL → HIGH_VOL | would be DEACTIVATED in LOW_VOL by the next activation_assigner --all; live eligibility differs from post-epoch verdict in LOW_VOL |
| S_aligned_economic_index_regime_timing | 12883→4333 | filter_ref/tier_liquid | 1.15→1.15 (+0.00) | 930→948 | LOW_VOL,TRANSITIONING → LOW_VOL,TRANSITIONING | no activation change |
| S_robust_min_variance_hedge | 12884→2691 | filter_ref/tier_r3000 | -0.16→-0.16 (-0.00) | 21785→22007 | CRISIS → CRISIS | no activation change |
| S12_insider | 12885→2691 | filter_ref/tier_r3000 | -0.23→-0.23 (-0.00) | 383→238 | ∅ → ∅ | ineligible in every regime before and after |
| S_schur_damped_minvar_shrinkage | 12884→12900 | none/— | -0.32→-0.32 (+0.00) | 69367→66266 | ∅ → ∅ | ineligible in every regime before and after |
| S_btc_momentum | 12883→12900 | none/— | 0.02→0.02 (+0.00) | 1504→1520 | HIGH_VOL → HIGH_VOL | no activation change |
| S_btc_halving_clock_cycle_timing | 12883→12900 | none/— | 0.35→0.35 (+0.00) | 1767→1767 | LOW_VOL → LOW_VOL | no activation change |
| S_gold_donchian_breakout_ema_filter | 12884→12900 | none/— | 1.17→1.17 (+0.00) | 347→347 | TRANSITIONING → TRANSITIONING | no activation change |
| S_covariance_shrinkage_minvar | —→4229 | cap/tier_liquid | —→-0.33 (—) | —→43957 | ∅ → CRISIS | no pre-epoch baseline |
| S_diversity_weighted_price_generated | —→4124 | cap/tier_liquid | —→-0.36 (—) | —→43312 | ∅ → ∅ | no pre-epoch baseline; live eligibility differs from post-epoch verdict in HIGH_VOL,CRISIS |
| S_pairs_trading_jump_diffusion_intraday | 4041→— | —/— | 0.10→— (—) | 5304→— | ∅ → ∅ | no post-epoch run yet — excluded from review |
