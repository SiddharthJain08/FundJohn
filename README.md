# John Bot — One-Person Hedge Fund (Claude Code)

A fully autonomous investment research system built on Claude Code, operated via Discord.

---

## Architecture Overview

```
Layer 1: CLAUDE.md          — Investment philosophy, checklist, kill criteria, verdict logic
Layer 2: .claude/commands/  — Diligence skill templates (/comps, /screen, /diligence-checklist, etc.)
Layer 3: MCP Servers        — Live data (SEC EDGAR, FRED, Yahoo Finance, Web Scraper)
Layer 4: Sub-Agents         — 5 parallel Claude Code processes (Bull, Bear, Mgmt, Filing, Revenue)
Layer 5: Worktrees          — Scenario lab (base/bull/bear git worktrees via scenario.sh)
Layer 6: Orchestrator       — Spawns Layer 4, receives from Layer 3+4, merges into memo
Layer 7: Diligence Memo     — 12-section structured output saved to output/memos/
         ↑
Discord ←→ johnbot/index.js — Command router and return loop
```

---

## Full Execution Flow

### `/diligence AAPL`

```
Discord (!john /diligence AAPL)
  ↓
johnbot/index.js  ← message received, ticker extracted
  ↓
Layer 6: scripts/orchestrator.js spawned as child process
  ↓
  ├── Layer 1: CLAUDE.md loaded (investment framework in cwd)
  ├── Layer 3: MCP servers available to all agents via .mcp.json
  │     ├── edgar   — SEC filings, Form 4, transcripts
  │     ├── fred    — macro data (rates, GDP, CPI)
  │     ├── yahoo-finance — prices, estimates, fundamentals
  │     └── puppeteer — web scraping, IR pages
  │
  └── Layer 4: 5 sub-agents spawn in parallel (each with MCP access)
        ├── Agent 1: Bull Case   → accelerating growth, expanding multiples
        ├── Agent 2: Bear Case   → decelerating growth, compressing multiples
        ├── Agent 3: Mgmt Credibility  → EDGAR 8-K/10-Q guidance vs. actuals
        ├── Agent 4: Filing Diff       → EDGAR 10-Q word-level diff
        └── Agent 5: Revenue Quality   → EDGAR + Yahoo customer concentration
              ↓ (all 5 complete or timeout at 5 min each)
        Layer 6: Orchestrator collects outputs
              ↓
        Layer 7: scripts/memo-template.md populated → 12-section memo
              ↓
        output/memos/AAPL-diligence-{timestamp}.md written
              ↓
johnbot/index.js  ← memo returned via stdout
  ↓
Discord: "✅ Diligence complete for AAPL — verdict: REVIEW"
         + memo attached as AAPL-diligence.md file
```

### `/comps AAPL`

```
Discord (!john /comps AAPL)
  ↓
johnbot/index.js  ← command detected
  ↓
Layer 2: .claude/commands/comps.md loaded, $ARGUMENTS → "AAPL"
  ↓
claude-bin -p "{expanded comps prompt}"  (cwd=/root/openclaw, Layer 3 MCP available)
  ↓
Discord: comp table as reply (or file attachment if > 8000 chars)
```

### `/scenario AAPL`

```
Discord (!john /scenario AAPL)
  ↓
johnbot/index.js  ← scenario handler
  ↓
Layer 5: scripts/scenario.sh AAPL
  ├── git worktree: scenarios/AAPL-base
  ├── git worktree: scenarios/AAPL-bull
  └── git worktree: scenarios/AAPL-bear
        ↓ (parallel analysis in each worktree)
output/memos/AAPL-scenario-comparison-{date}.md
  ↓
If diligence memo exists for AAPL → appended as Section 11
  ↓
Discord: comparison attached as file
```

---

## Project Structure

```
openclaw/
├── CLAUDE.md                          ← Layer 1: investment framework
├── .mcp.json                          ← Layer 3: MCP server config
├── .env.example                       ← API key placeholders
├── README.md                          ← this file
│
├── .claude/commands/                  ← Layer 2: skill templates
│   ├── comps.md                       ← /comps TICKER
│   ├── earnings-delta.md              ← /earnings-delta TICKER
│   ├── filing-diff.md                 ← /filing-diff TICKER
│   ├── mgmt-scorecard.md              ← /mgmt-scorecard TICKER
│   ├── diligence-checklist.md         ← /diligence-checklist TICKER
│   ├── scenario.md                    ← /scenario TICKER
│   └── screen.md                      ← /screen [params]
│
├── johnbot/                           ← Discord interface
│   ├── index.js                       ← bot + command router + return loop
│   ├── package.json
│   ├── johnbot.service                ← systemd unit
│   └── logs/
│       └── orchestrator.log           ← agent run history
│
├── scripts/
│   ├── orchestrator.js                ← Layer 6: parallel agent spawner + merger
│   ├── memo-template.md               ← Layer 7: 12-section memo structure
│   └── scenario.sh                    ← Layer 5: git worktree scenario lab
│
└── output/
    └── memos/                         ← saved diligence memos + scenario reports
        └── {TICKER}-diligence-{ts}.md
        └── {TICKER}-scenario-comparison-{date}.md
```

---

## Discord Commands

| Command | Description |
|---------|-------------|
| `!john /diligence AAPL` | Full 5-agent diligence → 12-section memo |
| `!john /comps AAPL` | Comparable company table |
| `!john /earnings-delta AAPL` | Revenue/EPS surprise + guidance delta |
| `!john /filing-diff AAPL` | 10-Q word-level diff, flags material changes |
| `!john /mgmt-scorecard AAPL` | 12-quarter guidance credibility score |
| `!john /diligence-checklist AAPL` | 6-item PASS/FAIL → PROCEED/REVIEW/KILL |
| `!john /screen sector=tech min_growth=15` | Quantitative screener |
| `!john /scenario AAPL` | Bull/base/bear scenario lab with worktrees |
| `!john /status` | Running agents, queue depth, last completed |
| `!john <any text>` | General Claude Code prompt |

---

## Setup

### 1. Install dependencies
```bash
cd /root/openclaw/johnbot && npm install
```

### 2. Configure API keys
```bash
cp .env.example .env
# Edit .env and fill in FRED_API_KEY, EDGAR_USER_AGENT
```

### 3. Enable the service
```bash
cp johnbot/johnbot.service ~/.config/systemd/user/
systemctl --user enable --now johnbot.service
```

### 4. Enable Message Content Intent
In Discord Developer Portal → your bot → Bot → Privileged Gateway Intents → enable **Message Content Intent**.

---

## Verdict Logic

| Score | Verdict | Meaning |
|-------|---------|---------|
| 6/6 PASS | PROCEED | All checklist items clear — position approved |
| 4–5/6 PASS | REVIEW | One or two items need resolution before proceeding |
| ≤ 3/6 PASS | KILL | Hard stop — do not proceed |

Any kill criterion triggers automatic KILL regardless of score.

---

## MCP Servers

| Server | Package | Data Provided |
|--------|---------|---------------|
| `edgar` | `mcp-server-edgar` | 10-K/10-Q/8-K/Form 4, full-text search |
| `fred` | `mcp-server-fred` | Interest rates, GDP, CPI, yield curves |
| `yahoo-finance` | `mcp-yahoo-finance` | Prices, consensus estimates, fundamentals |
| `puppeteer` | `@modelcontextprotocol/server-puppeteer` | Web scraping, IR pages |

Servers install automatically via `npx -y` on first use. Set `FRED_API_KEY` in `.env`.
