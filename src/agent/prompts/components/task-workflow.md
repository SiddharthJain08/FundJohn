# Task Workflow

## Default Execution Pattern: Plan → Validate → Execute → Report

### 1. Plan
Before any execution, output a brief plan (3-5 bullets max):
- What data is needed and from which provider tier
- Which subagents will run and in what order (parallel vs sequential)
- What the success condition looks like
- Check verdict_cache — if fresh results exist, skip straight to Report

### 2. Validate Data
- Confirm required data sources are reachable before spawning subagents
- Check preferences.json for operator thresholds (never hardcode)
- Verify portfolio.json last_verified_at if trade pipeline is involved
- If data validation fails → abort with structured error, do NOT improvise

### 3. Execute
- Spawn parallel research subagents (research) using swarm.js
- For trade pipelines: sequential — data-prep → [GATE] → compute → equity-analyst → report-builder
- All heavy data processing in Python (PTC mode). Never dump raw API JSON into context.
- Write intermediate results to work/<task>/data/ as CSV/parquet

### 4. Report
- Compile subagent outputs into the structured memo or trade report
- Save to results/{ticker}-{date}-{type}.md
- Write verdict_cache JSON to .agents/verdict-cache/{ticker}-{date}.json
- Post Discord summary (<2000 chars) + attach full report

## Flash Mode (quick queries)
For simple lookups (quote, status, portfolio view):
- Use JSON snapshot tools directly — no subagents, no sandbox
- Return answer in <10 seconds
- Trigger PTC mode if the answer requires data processing or cross-source comparison
