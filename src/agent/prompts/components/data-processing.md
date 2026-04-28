# Data Processing

## PTC Pattern — All Data Processing in Python

Never process financial data in context (LLM math). Write Python. Execute. Read conclusions.

### Standard Python Imports (sandbox)
```python
import pandas as pd
import numpy as np
from tools.fmp import get_financial_statements, get_key_metrics, get_profile, get_peers
from tools.polygon import get_prices, get_snapshot, get_rsi, get_sma, get_bbands, get_sector
from tools.sec_edgar import get_filing, get_submissions
from tools.tavily import search
from tools.yahoo import get_options, get_insider_transactions  # fallback only
from tools.validate import validate_manifest
from tools._rate_limiter import _call_mcp
# Note: AlphaVantage was removed 2026-04-28. Technical indicators
# (RSI/SMA/EMA/BBands) and sector data now come from Polygon. Macro /
# economic calendar comes from FMP.
```

### Data Flow
```
API call (Python, rate-limited) 
  → raw data (never shown to agent context)
  → pandas DataFrame
  → clean/validate
  → save to work/<task>/data/<filename>.csv or .parquet
  → return ONLY summary metrics to context
```

### Artifact System
Large outputs go to filesystem — never to context:
- DataFrames → `work/<task>/data/<name>.csv`
- Price series → `work/<task>/data/<name>.parquet`
- Charts → `work/<task>/charts/<name>.png`
- Scenarios → `work/<task>/data/scenarios.csv`

Always write DATA_MANIFEST.json after data-prep:
```json
{
  "task": "AAPL-diligence",
  "created_at": "2026-04-07T12:00:00Z",
  "files": {
    "financials.csv": { "rows": 16, "periods": "2022Q1-2025Q4" },
    "comps.csv": { "rows": 8, "peers": ["MSFT","GOOGL","META","AMZN","NVDA","CRM","ADBE","NOW"] },
    "prices.parquet": { "rows": 252, "ticker": "AAPL", "days": 252 }
  }
}
```

### Data Validation Gate
MANDATORY before compute and equity-analyst subagents.
The validate_manifest() function in tools/validate.py checks:
- Required files exist per DATA_MANIFEST.json
- Schema columns match expected fields
- Anomaly rules pass (no zero/negative revenue, out-of-range margins, negative EV)

If validation fails: emit [DATA VALIDATION FAILED — {reason}] and HALT.
Do NOT improvise around missing data. Restart from data-prep.

### Rate Limiting Rule
ALL MCP calls go through `_call_mcp()`. This function checks the Redis token bucket
for the provider before calling. If bucket is empty, it sleeps 0.5s and retries.
Never bypass the rate limiter. Never throw on rate limit — backoff and retry.
