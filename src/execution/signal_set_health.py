# src/execution/signal_set_health.py
# Pure gate for the intraday-redeploy signal-set-health check (W3 F2a): is the active
# signal set abnormally thin vs a recent baseline? If so the redeploy is likely acting on
# bad/incomplete data and must NOT orphan-close the book. No DB, no I/O.
def recent_baseline(counts):
    vals = sorted(float(c) for c in (counts or []))
    n = len(vals)
    if n == 0:
        return 0.0
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0

def is_signal_set_thin(current_count, baseline_count, floor, frac):
    threshold = max(float(floor), float(frac) * float(baseline_count)) if baseline_count and baseline_count > 0 else float(floor)
    return float(current_count) < threshold
