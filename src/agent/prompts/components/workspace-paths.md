# Workspace Paths

## Directory Layout

```
workspace/
├── agent.md                    # Persistent memory — injected every LLM call
├── work/
│   └── {ticker}-{task}/
│       ├── data/
│       │   ├── DATA_MANIFEST.json   # Required by validation gate
│       │   ├── financials.csv
│       │   ├── comps.csv
│       │   ├── prices.parquet
│       │   └── scenarios.csv
│       └── charts/
├── results/                    # Finalized reports — never overwrite
│   ├── {ticker}-{date}-memo.md
│   ├── {ticker}-{date}-trade.md
│   └── {ticker}-{date}-veto.md
├── data/                       # Shared datasets across threads
├── tools/                      # Auto-generated MCP Python modules (READ-ONLY)
│   ├── _rate_limiter.py
│   ├── validate.py
│   ├── fmp.py
│   ├── polygon.py
│   ├── alpha_vantage.py
│   ├── sec_edgar.py
│   ├── tavily.py
│   ├── yahoo.py
│   └── docs/
└── .agents/
    ├── threads/
    │   └── {tid}/
    │       ├── evicted/         # Large results evicted from context
    │       └── history/
    ├── skills/
    │   └── skills-lock.json
    ├── verdict-cache/           # Structured verdict JSON per ticker
    │   └── {ticker}-{date}.json
    └── user/
        ├── portfolio.json       # Operator-maintained, includes last_verified_at
        ├── watchlist.json
        └── preferences.json     # Operator-configurable thresholds
```

## Naming Conventions
- Task dirs: `{TICKER}-{task-type}` e.g. `AAPL-diligence`, `Q2-rebalance`
- Results: `{TICKER}-{YYYY-MM-DD}-{type}.md` — NEVER overwrite an existing result file
- Verdict cache: `{TICKER}-{YYYY-MM-DD}.json`
- Data files: lowercase with underscores (`financials.csv`, not `Financials.csv`)

## Read-Only Paths
- `tools/` — auto-generated, never edit manually
- `.agents/user/portfolio.json` — operator-maintained only

## Agent.md Updates
After every task, update the relevant sections of agent.md:
- Goals (mark completed)
- Active Threads (update status)
- File Index (list new files written)
- Lessons Learned (add if something went wrong or worked well)
