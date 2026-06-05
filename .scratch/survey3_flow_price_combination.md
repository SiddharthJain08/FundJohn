# SP-6 B-flow Literature Survey 3/3 — Combining correlated flow+price signals

## Verified citations (abstracts fetched)
1. Cont, Kukanov, Stoikov (2014, J Financial Econometrics; arXiv 1011.6402) — price change ≈ β·OFI + ε over short intervals; β inversely ∝ market depth. THE foundation for residual displacement = vwap_disp − β·OFI.
2. Bacry & Muzy (2013, arXiv 1301.1135) — multivariate Hawkes; 4 kernels; TICK-level event data.
3. Anantha & Jain (2024, arXiv 2408.03594) — Hawkes (sum-of-exp kernel) best OFI forecast; TICK data (NSE).
4. Kakushadze (2015, arXiv 1501.05381) — Combining Alphas via Bounded Regression; bounds control overfitting when #signals large vs history. Use for overfitting-control, NOT inverse-cov.
5. Kakushadze & Yu (2016/2017, arXiv 1603.05937) — billion alphas, LARGE-N weighting only. Do NOT cite for collinearity (abstract contradicts).
6. DeMiguel, Garlappi, Uppal (2009, RFS 22:1915) — 1/N beats 14 optimized models OOS; needs ~3000mo (25 assets) for optimization to win. CANONICAL equal-weight robustness.
7. Taranto, Bormetti, Bouchaud, Lillo, Toth (2016, arXiv 1602.02735) — propagator / transient (reverting) vs permanent impact. Grounds WHY resid_disp reverts.

## Power calc (per-session IC sd = 0.07)
SE(mean IC) = 0.07/√n. n=31 → SE≈0.0126 → |t|≥3 needs mean IC ≥ 0.038; |t|≥2 needs ≥0.025.
n to detect true mu at |t|≥3: n ≥ (3·0.07/mu)² = (0.21/mu)².
 mu=0.05→n≈18; 0.04→n≈28; 0.034 (current best single)→n≈38; 0.03→n≈49; 0.02→n≈110.
Current best single-feature mean IC ≈ 0.034 (t≈2.6–2.8 at n=34). Joint model must lift past ~0.038 — OOS question.

## Synthesis insight
Operator's literal ask = 2-condition GATE (flow cluster AND price moved lower) = +DOF trap.
Continuous residual displacement (resid_disp = vwap_disp − β·OFI = "absorption surprise") encodes
identical economics with ONE param, no gate. Recommend continuous form. Transient-impact decomp
(Taranto et al.) says temporary impact reverts → resid ≈ transient → reversion is the prior.
