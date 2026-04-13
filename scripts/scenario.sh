#!/usr/bin/env bash
# scenario.sh — Scenario Lab: Create 3 git worktree branches for bull/base/bear analysis
# Usage: ./scripts/scenario.sh <TICKER>
set -euo pipefail

TICKER="${1:-}"
if [[ -z "$TICKER" ]]; then
  echo "Usage: $0 <TICKER>"
  echo "Example: $0 AAPL"
  exit 1
fi

TICKER="${TICKER^^}"  # uppercase
WORKDIR="$(cd "$(dirname "$0")/.." && pwd)"
SCENARIOS_DIR="${WORKDIR}/scenarios"
CLAUDE_BIN="${CLAUDE_BIN:-/usr/local/bin/claude-bin}"
CLAUDE_UID="${CLAUDE_UID:-1001}"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Scenario Lab — ${TICKER}"
echo "  Working dir: ${WORKDIR}"
echo "═══════════════════════════════════════════════════"
echo ""

# ── Git repo check ────────────────────────────────────────────────────────────
cd "${WORKDIR}"

if ! git rev-parse --git-dir > /dev/null 2>&1; then
  echo "Initializing git repository in ${WORKDIR}..."
  git init
  git add -A
  git commit -m "Initial commit: John Bot hedge fund architecture" --allow-empty
  echo "Git repo initialized."
fi

# ── Clean up any existing scenario worktrees for this ticker ─────────────────
for SCENARIO in base bull bear; do
  BRANCH="scenarios/${TICKER}-${SCENARIO}"
  WT_PATH="${SCENARIOS_DIR}/${TICKER}-${SCENARIO}"

  if git worktree list | grep -q "${WT_PATH}"; then
    echo "Removing existing worktree: ${WT_PATH}"
    git worktree remove --force "${WT_PATH}" 2>/dev/null || true
  fi

  if git branch --list "${BRANCH}" | grep -q "${BRANCH}"; then
    echo "Removing existing branch: ${BRANCH}"
    git branch -D "${BRANCH}" 2>/dev/null || true
  fi
done

mkdir -p "${SCENARIOS_DIR}"

# ── Create 3 worktrees ────────────────────────────────────────────────────────
CURRENT_BRANCH=$(git branch --show-current 2>/dev/null || echo "main")

for SCENARIO in base bull bear; do
  BRANCH="scenarios/${TICKER}-${SCENARIO}"
  WT_PATH="${SCENARIOS_DIR}/${TICKER}-${SCENARIO}"

  echo "Creating worktree: ${BRANCH} → ${WT_PATH}"
  git worktree add -b "${BRANCH}" "${WT_PATH}" HEAD
done

echo ""
echo "✅ Worktrees created:"
git worktree list | grep "scenarios/${TICKER}"
echo ""

# ── Write scenario assumption files ──────────────────────────────────────────

write_assumptions() {
  local scenario="$1"
  local wt_path="${SCENARIOS_DIR}/${TICKER}-${scenario}"
  local file="${wt_path}/scenarios/${TICKER}-assumptions.md"
  mkdir -p "${wt_path}/scenarios"

  case "$scenario" in
    base)
      cat > "${file}" <<EOF
# ${TICKER} — Base Case Assumptions
*Scenario: Base | Branch: scenarios/${TICKER}-base*

## Revenue Model
- Revenue growth: Current trajectory, no material acceleration or deceleration
- Gross margin: Stable at current level ± 50bps/year
- EBITDA margin: Gradual improvement of 100-150bps/year

## Multiple Assumptions
- EV/NTM Revenue: Peer median multiple (no re-rating)
- Entry: Current price
- Exit multiple: Same as entry (no compression or expansion)

## Implied Return
- 1-year price target: \$XX (base case — to be filled by analysis)
- IRR: ~X% (to be calculated)

## Key Assumptions to Validate
1. Revenue growth sustains at current rate
2. No new competitive threats materialize
3. Macro environment stable
EOF
      ;;

    bull)
      cat > "${file}" <<EOF
# ${TICKER} — Bull Case Assumptions
*Scenario: Bull | Branch: scenarios/${TICKER}-bull*

## Revenue Model — Bull Scenario
- Revenue growth: ACCELERATING — model +300-500bps above current growth rate
- Gross margin: EXPANDING — +100-200bps/year from operating leverage
- EBITDA margin: Faster path to profitability or above-consensus expansion

## Multiple Assumptions — Expansion
- EV/NTM Revenue: 20-30% premium to current (re-rating on growth acceleration)
- Justify vs. best-in-class peers at peak multiple
- Catalyst for expansion: [specify from diligence]

## Implied Return — Bull
- 1-year price target: \$XX (bull case — to be filled)
- Upside vs. base: +X%

## Bull Case Key Assumptions
1. Revenue accelerates beyond current consensus
2. Gross margin expands via pricing power or mix shift
3. Multiple re-rates to growth premium tier
4. No material new competitive threats
5. Macro tailwind or rate cut benefits multiple
EOF
      ;;

    bear)
      cat > "${file}" <<EOF
# ${TICKER} — Bear Case Assumptions
*Scenario: Bear | Branch: scenarios/${TICKER}-bear*

## Revenue Model — Bear Scenario
- Revenue growth: DECELERATING — model -300-500bps below current growth rate
- Gross margin: COMPRESSING — -50 to -150bps/year from competitive pressure
- EBITDA margin: Profitability timeline pushes out 2+ quarters

## Multiple Assumptions — Compression
- EV/NTM Revenue: 20-30% discount to current (de-rating on growth deceleration)
- Trough multiple: sector floor during down-cycle (reference historical comps)
- Trigger for compression: [specify from diligence]

## Implied Return — Bear
- 1-year price target: \$XX (bear case — to be filled)
- Downside vs. base: -X%

## Bear Case Key Assumptions
1. Revenue decelerates materially below current consensus
2. Competitive pressure compresses margins
3. Multiple de-rates to below-sector-median level
4. Macro headwind (rate sensitivity, demand pull-forward unwind)
5. One or more kill criteria triggered
EOF
      ;;
  esac

  echo "  Wrote: ${file}"
}

write_assumptions base
write_assumptions bull
write_assumptions bear

# ── Commit assumption files to each branch ────────────────────────────────────
for SCENARIO in base bull bear; do
  WT_PATH="${SCENARIOS_DIR}/${TICKER}-${SCENARIO}"
  pushd "${WT_PATH}" > /dev/null
  git add "scenarios/${TICKER}-assumptions.md"
  git commit -m "chore: ${TICKER} ${SCENARIO} scenario assumptions" --allow-empty-message 2>/dev/null || true
  popd > /dev/null
done

# ── Run analysis in each worktree (as claudebot user) ─────────────────────────
echo ""
echo "Running scenario analysis in each worktree..."
echo "(This spawns Claude Code sub-agents — may take several minutes)"
echo ""

PIDS=()
for SCENARIO in base bull bear; do
  WT_PATH="${SCENARIOS_DIR}/${TICKER}-${SCENARIO}"
  LOG_FILE="${WORKDIR}/output/memos/${TICKER}-${SCENARIO}-scenario.md"

  PROMPT="You are analyzing the ${SCENARIO} scenario for ${TICKER}. The assumptions file is at scenarios/${TICKER}-assumptions.md.

Read the assumptions, then:
1. Build a 2-year financial model using the scenario assumptions (revenue, gross margin, EBITDA, FCF)
2. Apply appropriate entry and exit multiples per the assumptions
3. Calculate implied share price and return (1yr and 2yr)
4. Identify the 3 most important variables that could prove the scenario right or wrong
5. Output a structured markdown table with all projections

Format your output as clean markdown suitable for a side-by-side comparison report."

  echo "  ▶ Spawning ${SCENARIO} analysis..."
  sudo -u claudebot env HOME=/home/claudebot OPENCLAW_DIR="${WT_PATH}" \
    "${CLAUDE_BIN}" --dangerously-skip-permissions -p "${PROMPT}" \
    > "${LOG_FILE}" 2>&1 &
  PIDS+=($!)
done

# Wait for all 3 to complete
echo ""
echo "Waiting for scenario analyses to complete..."
for PID in "${PIDS[@]}"; do
  wait "$PID" || true
done

echo ""
echo "✅ Scenario analyses complete."

# ── Produce side-by-side comparison ──────────────────────────────────────────
COMPARE_FILE="${WORKDIR}/output/memos/${TICKER}-scenario-comparison-$(date +%Y%m%d).md"

cat > "${COMPARE_FILE}" <<EOF
# Scenario Comparison — ${TICKER}
*Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)*

| | Base | Bull | Bear |
|--|------|------|------|
| Rev Growth Assumption | Stable | Accelerating (+3-5pp) | Decelerating (-3-5pp) |
| Gross Margin | Stable | Expanding | Compressing |
| Exit Multiple | Peer median | 20-30% premium | 20-30% discount |
| Price Target | \$XX | \$XX | \$XX |
| Return vs. Today | +X% | +X% | -X% |

---

## Base Case Output
$(cat "${WORKDIR}/output/memos/${TICKER}-base-scenario.md" 2>/dev/null || echo '*Analysis pending*')

---

## Bull Case Output
$(cat "${WORKDIR}/output/memos/${TICKER}-bull-scenario.md" 2>/dev/null || echo '*Analysis pending*')

---

## Bear Case Output
$(cat "${WORKDIR}/output/memos/${TICKER}-bear-scenario.md" 2>/dev/null || echo '*Analysis pending*')

---

## Merge Instructions

To accept the winning scenario back to main:

\`\`\`bash
# Merge base
git merge scenarios/${TICKER}-base

# Or cherry-pick the assumptions file only
git checkout main
git checkout scenarios/${TICKER}-bull -- scenarios/${TICKER}-assumptions.md
git commit -m "feat: adopt ${TICKER} bull scenario assumptions"
\`\`\`

## Cleanup

\`\`\`bash
# Remove all scenario worktrees and branches when done
for s in base bull bear; do
  git worktree remove --force scenarios/${TICKER}-\$s
  git branch -D scenarios/${TICKER}-\$s
done
\`\`\`
EOF

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Scenario Lab Complete — ${TICKER}"
echo ""
echo "  Comparison report: ${COMPARE_FILE}"
echo ""
echo "  Worktrees:"
git worktree list | grep "scenarios/${TICKER}" || true
echo ""
echo "  To open comparison:"
echo "    cat ${COMPARE_FILE}"
echo "═══════════════════════════════════════════════════"
echo ""
