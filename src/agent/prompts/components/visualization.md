# Visualization

## Chart Generation (Python in sandbox)

All charts are generated in Python and saved to work/<task>/charts/.
Embed in memos via relative path: `![EV/Revenue Comps](../work/AAPL-diligence/charts/ev_revenue_comps.png)`

### Standard Chart Types

**Comp Table Chart** — EV/Revenue vs Revenue Growth scatter
```python
import matplotlib.pyplot as plt
import pandas as pd

df = pd.read_csv("work/<task>/data/comps.csv")
fig, ax = plt.subplots(figsize=(10, 6))
ax.scatter(df["revenue_growth"], df["ev_revenue"], s=80, color="steelblue")
for _, row in df.iterrows():
    ax.annotate(row["ticker"], (row["revenue_growth"], row["ev_revenue"]))
ax.set_xlabel("Revenue Growth (%)")
ax.set_ylabel("EV/Revenue (x)")
ax.set_title(f"EV/Revenue vs Growth — {ticker} vs Peers")
plt.tight_layout()
plt.savefig("work/<task>/charts/ev_revenue_comps.png", dpi=150)
plt.close()
```

**Price History** — 1-year price with MA overlays
**Scenario Table** — bull/base/bear side-by-side bar
**Risk Contribution** — portfolio drawdown impact bar chart

### Rules
- Save at dpi=150 minimum
- Always close figure after saving (memory management)
- Filename convention: `{chart_type}_{ticker}.png`
- Never render charts to context — always file → embed path
- Charts are optional artifacts — missing charts never block the pipeline
