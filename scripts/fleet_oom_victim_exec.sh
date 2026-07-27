#!/usr/bin/env bash
# Exec shim: make ONLY the backtest child the kernel's first OOM victim
# (oom_score_adj=1000), leaving the node driver + wrapper at normal priority.
# Unit-level OOMScoreAdjust=1000 (2026-07-27) marked the DRIVER too — at the
# 13:30Z market-open memory surge the kernel killed the driver itself and the
# unit died mid-fleet (OOMPolicy=continue only survives CHILD kills, not the
# main process). This shim restores the intended semantics: a strategy OOM
# kills THAT strategy; the fleet continues.
echo 1000 > /proc/self/oom_score_adj 2>/dev/null || true
exec nice -n 19 "$@"
