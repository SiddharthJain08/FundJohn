# Phase-1d MA-reversion policy — report

sessions: 813; rows: 2373108; excluded(thin null): 16

| leg | zeta | mean_excess_bps | t | n_sessions | trig_rate | mean_dvs_dump | pol_p95_adv | pool_p95_adv |
|---|---|---|---|---|---|---|---|---|
| LONG | 1.0 | -0.295 | -0.15 | 813 | 0.822 | -0.94 | 199.1 | 191.7 |
| LONG | 1.5 | -0.143 | -0.08 | 813 | 0.749 | -0.04 | 187.7 | 180.1 |
| LONG | 2.0 | -0.574 | -0.44 | 813 | 0.564 | -0.27 | 176.3 | 165.5 |
| SHORT | 1.0 | -0.467 | -0.24 | 813 | 0.821 | 0.93 | 186.9 | 180.8 |
| SHORT | 1.5 | -0.586 | -0.35 | 813 | 0.741 | 1.10 | 178.1 | 170.3 |
| SHORT | 2.0 | -0.641 | -0.50 | 813 | 0.540 | 1.13 | 169.0 | 157.7 |

## Diagnostics (non-gating, spec §3)

- LONG z=1.0: fallback=0.178; P(adv>5/10/25bps)=0.459/0.434/0.364; entry-minute d10/50/90=[31, 40, 157]
    - 2023H1: mean_excess=4.676bps (n=124)
    - 2023H2: mean_excess=-3.568bps (n=126)
    - 2024H1: mean_excess=3.006bps (n=124)
    - 2024H2: mean_excess=-4.310bps (n=128)
    - 2025H1: mean_excess=3.160bps (n=122)
    - 2025H2: mean_excess=-3.681bps (n=128)
    - 2026Q1: mean_excess=-1.725bps (n=61)
- LONG z=1.5: fallback=0.251; P(adv>5/10/25bps)=0.453/0.426/0.353; entry-minute d10/50/90=[35, 53, 247]
    - 2023H1: mean_excess=4.133bps (n=124)
    - 2023H2: mean_excess=-3.280bps (n=126)
    - 2024H1: mean_excess=2.200bps (n=124)
    - 2024H2: mean_excess=-4.229bps (n=128)
    - 2025H1: mean_excess=3.723bps (n=122)
    - 2025H2: mean_excess=-2.492bps (n=128)
    - 2026Q1: mean_excess=-1.344bps (n=61)
- LONG z=2.0: fallback=0.436; P(adv>5/10/25bps)=0.450/0.420/0.342; entry-minute d10/50/90=[41, 75, 311]
    - 2023H1: mean_excess=2.124bps (n=124)
    - 2023H2: mean_excess=-3.245bps (n=126)
    - 2024H1: mean_excess=1.077bps (n=124)
    - 2024H2: mean_excess=-3.293bps (n=128)
    - 2025H1: mean_excess=1.871bps (n=122)
    - 2025H2: mean_excess=-1.506bps (n=128)
    - 2026Q1: mean_excess=-1.124bps (n=61)
- SHORT z=1.0: fallback=0.179; P(adv>5/10/25bps)=0.477/0.451/0.378; entry-minute d10/50/90=[31, 40, 161]
    - 2023H1: mean_excess=-5.698bps (n=124)
    - 2023H2: mean_excess=2.423bps (n=126)
    - 2024H1: mean_excess=-2.363bps (n=124)
    - 2024H2: mean_excess=3.702bps (n=128)
    - 2025H1: mean_excess=-2.955bps (n=122)
    - 2025H2: mean_excess=2.034bps (n=128)
    - 2026Q1: mean_excess=-0.964bps (n=61)
- SHORT z=1.5: fallback=0.259; P(adv>5/10/25bps)=0.473/0.445/0.368; entry-minute d10/50/90=[35, 54, 252]
    - 2023H1: mean_excess=-5.094bps (n=124)
    - 2023H2: mean_excess=1.801bps (n=126)
    - 2024H1: mean_excess=-2.423bps (n=124)
    - 2024H2: mean_excess=2.597bps (n=128)
    - 2025H1: mean_excess=-2.513bps (n=122)
    - 2025H2: mean_excess=1.858bps (n=128)
    - 2026Q1: mean_excess=-0.575bps (n=61)
- SHORT z=2.0: fallback=0.460; P(adv>5/10/25bps)=0.466/0.435/0.354; entry-minute d10/50/90=[41, 75, 312]
    - 2023H1: mean_excess=-3.021bps (n=124)
    - 2023H2: mean_excess=0.828bps (n=126)
    - 2024H1: mean_excess=-1.594bps (n=124)
    - 2024H2: mean_excess=1.373bps (n=128)
    - 2025H1: mean_excess=-2.559bps (n=122)
    - 2025H2: mean_excess=1.001bps (n=128)
    - 2026Q1: mean_excess=-0.739bps (n=61)

**VERDICT: LONG=FAIL SHORT=FAIL**

Quantization note (plan A1): guardrail pool p95 read from a 0.1bps-bin histogram — accepted approximation vs the pre-registered 10bps margin.

Linkage (spec §0): PASS authorizes the FORWARD SHADOW LANE only — never live cutover. FAIL closes the idea at minute scale. PASS-WITH-TAIL-BREACH does not authorize the lane.