# Phase-1b historical kill-test verdict

ic_grid: analysis/bflow_phase1b_hist/bflow_phase1b_ic_grid.parquet
sessions in file: 813; ELIGIBLE (prereg SS4): 813

## Pooled grid (all eligible sessions)
```
cell                           mean_ic       t     n
ofi_5|ret_to_dump              -0.0236  -30.66   813
ofi_5|ret_fwd_5                -0.0168  -16.32   813
ofi_5|ret_fwd_15               -0.0157  -14.75   813
ofi_5|ret_fwd_30               -0.0158  -14.98   813
ofi_5|ret_fwd_60               -0.0162  -16.19   813
ofi_15|ret_to_dump             -0.0353  -27.54   813
ofi_15|ret_fwd_5               -0.0122  -12.17   813
ofi_15|ret_fwd_15              -0.0158  -11.17   813
ofi_15|ret_fwd_30              -0.0176  -11.35   813
ofi_15|ret_fwd_60              -0.0213  -13.52   813
vwap_disp_30|ret_to_dump       -0.0472  -23.36   813
vwap_disp_30|ret_fwd_5         -0.0206  -14.69   813
vwap_disp_30|ret_fwd_15        -0.0241  -11.88   813
vwap_disp_30|ret_fwd_30        -0.0257  -10.85   813
vwap_disp_30|ret_fwd_60        -0.0306  -12.69   813
```

## 2023H1 (124 sessions) — PRIMARY
```
cell                           mean_ic       t     n
ofi_5|ret_to_dump              -0.0290  -14.75   124
ofi_15|ret_to_dump             -0.0441  -13.18   124
vwap_disp_30|ret_to_dump       -0.0563  -10.91   124
```

## 2023H2 (126 sessions) — PRIMARY
```
cell                           mean_ic       t     n
ofi_5|ret_to_dump              -0.0279  -14.91   126
ofi_15|ret_to_dump             -0.0431  -13.55   126
vwap_disp_30|ret_to_dump       -0.0557  -10.92   126
```

## 2024H1 (124 sessions) — PRIMARY
```
cell                           mean_ic       t     n
ofi_5|ret_to_dump              -0.0210  -12.34   124
ofi_15|ret_to_dump             -0.0311  -10.79   124
vwap_disp_30|ret_to_dump       -0.0419   -9.76   124
```

## 2024H2 (128 sessions) — PRIMARY
```
cell                           mean_ic       t     n
ofi_5|ret_to_dump              -0.0215  -11.40   128
ofi_15|ret_to_dump             -0.0313   -9.79   128
vwap_disp_30|ret_to_dump       -0.0401   -7.90   128
```

## 2025H1 (122 sessions) — PRIMARY
```
cell                           mean_ic       t     n
ofi_5|ret_to_dump              -0.0271  -10.78   122
ofi_15|ret_to_dump             -0.0410   -9.99   122
vwap_disp_30|ret_to_dump       -0.0612   -9.14   122
```

## 2025H2 (128 sessions) — PRIMARY
```
cell                           mean_ic       t     n
ofi_5|ret_to_dump              -0.0179  -10.57   128
ofi_15|ret_to_dump             -0.0251   -9.40   128
vwap_disp_30|ret_to_dump       -0.0328   -7.96   128
```

## 2026Q1 (61 sessions) — PRIMARY
```
cell                           mean_ic       t     n
ofi_5|ret_to_dump              -0.0187   -7.75    61
ofi_15|ret_to_dump             -0.0277   -7.33    61
vwap_disp_30|ret_to_dump       -0.0388   -5.98    61
```

## VERDICT

- pooled PRIMARY cells with |t|>=3 (either sign): ['ofi_5|ret_to_dump', 'ofi_15|ret_to_dump', 'vwap_disp_30|ret_to_dump']
- recent-bucket (2025H2/2026Q1) PRIMARY |t|>=2: True
- eligible sessions: 813 (KILL floor 700)

**VERDICT: SURVIVE-STRONG**

Linkage (prereg SS0/SS5): KILL => minute-scale flow channel closed, July forward decision pre-empted. Any SURVIVE => the registered forward gate (n_oos>=20, sessions >=2026-06-08) remains the SOLE PASS arbiter — no historical outcome can pass.
