# SP-7 Phase A — Grounding Snapshot

**Date:** 2026-06-04  
**Task:** SP-7 Phase A, Task 0 — Pre-flight verification of facts later tasks depend on

---

## Verification Checklist

### (a) Next Migration Number is Free

**Command:**
```bash
ls src/database/migrations/ | sort -t_ -k1 -n | tail -3
```

**Output:**
```
125_param_change_backtest_cols.sql
126_sp6_overnight_signal_state.sql
128_option_hedge_ledger.sql
```

**Verdict:** ✅ PASS  
**Interpretation:** Migration 128 is the max. Task 4 assumes 129 is available. No collision detected.

---

### (b) CIK Map Present + Format

**Command:**
```bash
python3 -c "import json; m=json.load(open('/root/openclaw/data/master/_sec_ticker_cik.json')); print(len(m), list(m.items())[:2])"
```

**Output:**
```
10371 [('NVDA', '0001045810'), ('GOOGL', '0001652044')]
```

**Verdict:** ✅ PASS  
**Interpretation:** File exists at `/root/openclaw/data/master/_sec_ticker_cik.json`. Contains 10,371 ticker→zero-padded-CIK-string pairs. Format matches expectation: `('NVDA', '0001045810')` with 10-digit zero-padded strings.

---

### (c) Corporate Actions Split Rows Exist

**Command:**
```bash
python3 -c "import pandas as pd; df=pd.read_parquet('/root/openclaw/data/master/corporate_actions.parquet', columns=['action_type']); print(df.action_type.value_counts().to_dict())"
```

**Output:**
```
{'cash_dividend': 24, 'forward_split': 1}
```

**Verdict:** ✅ PASS  
**Interpretation:** Parquet file contains 24 cash_dividend rows and 1 forward_split row. Both action types present as expected.

---

### (d) Daily Metadata Chain

**Command:**
```bash
grep -n "run_ticker_metadata_step" src/maintenance/refresh_tradable_universe.py
```

**Output:**
```
282:        ["python3", "-m", "src.pipeline.run_ticker_metadata_step"],
```

**Verdict:** ✅ PASS  
**Interpretation:** Found at line 282 in `refresh_tradable_universe.py`. Subprocess call exists in expected location (lines ~277-282 range).

---

### (e) Collector Adjustment Flag

**Command:**
```bash
grep -n "'--adjustment', 'all'" src/pipeline/collector.js
```

**Output:**
```
589:    '--adjustment', 'all',
```

**Verdict:** ✅ PASS  
**Interpretation:** Found at line 589 inside `fillPricesAlpaca` (expected line ~589). Collector has the `--adjustment all` flag configured.

---

## Summary

| Check | Status | Finding |
|-------|--------|---------|
| (a) Migration 129 free | ✅ PASS | Max is 128; 129 available |
| (b) CIK map present | ✅ PASS | 10,371 tickers; correct format |
| (c) Corporate actions splits | ✅ PASS | 1 forward_split row present |
| (d) Metadata chain | ✅ PASS | Line 282 call exists |
| (e) Collector adjustment flag | ✅ PASS | Line 589 configured |

**Overall Status:** ✅ DONE — All live-state facts verified. No mismatches. Phase A plan assumptions are grounded.
