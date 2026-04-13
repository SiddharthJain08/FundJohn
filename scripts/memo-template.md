# MEMO TEMPLATE — DEPRECATED
#
# The orchestrator (scripts/orchestrator.js) no longer uses this template.
# Memos are now assembled by direct concatenation in assembleMemo().
# Agent outputs are inserted verbatim — no summarization, no LLM compilation.
#
# Format produced by assembleMemo():
#
# DILIGENCE MEMO — {TICKER}
# Date: {date} | Run: {id} | Elapsed: {Xs}
#
# === VERDICT ===
# {PROCEED|REVIEW|KILL} ({N}/6 passed) — {reasoning}
#
# === CHECKLIST ===
# | # | Item | Result | Data |
# (6 rows, evaluated deterministically by evaluateChecklist())
#
# === SIGNALS ===
# (kill/warning tags from ---SIGNALS:--- lines in agent blocks)
#
# === BULL CASE ===
# (verbatim ---AGENT:bull--- block)
#
# === BEAR CASE ===
# (verbatim ---AGENT:bear--- block)
#
# === MANAGEMENT ===
# (verbatim ---AGENT:mgmt--- block)
#
# === FILINGS ===
# (verbatim ---AGENT:filing--- block)
#
# === REVENUE ===
# (verbatim ---AGENT:revenue--- block)
#
# === SCENARIO ===
# (scenario comparison if run, else placeholder)
#
# === AGENT LOG ===
# (status/elapsed per agent)
