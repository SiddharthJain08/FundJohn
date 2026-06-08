# Deep-Dip Buy-Leg — Post-Verdict Robustness (DIP = 50 bps)

The pre-registered verdict is **KILL** (session-clustered blended improvement
−4.7226 bps, t −2.207, n=813). This note interrogates that number so the KILL is
not mistaken for a clustering artifact. Read-only; does not alter the verdict.

## The blended is more negative than either conditional split — why

| statistic | blended | fill-only | no-fill | reconciles? |
|-----------|---------|-----------|---------|-------------|
| **pooled** (event-weighted) | −5.0938 | −7.5688 | −0.8403 | yes: 0.632·(−7.57)+0.368·(−0.84) = −5.09 |
| **clustered** (session-weighted) | −4.7226 | −0.1310 | −1.1693 | — |

The pooled figures reconcile to the cent (machinery confirmed correct). The
clustered fill mean (−0.13) and pooled fill mean (−7.57) diverge by design:
**high-volatility sessions produce both more 50-bps dips (many filled events) and
worse post-dip continuation.** Event-weighted, those days dominate (−7.57);
session-weighted, each is one vote (−0.13). The clustered blended (−4.72) is
dragged below both clustered conditionals by the heavy left tail of such days.

## Distribution of per-session blended improvement

mean −4.72 · median **−1.79** · p05 −99.4 · p95 +70.8 · min −503 · max +632
sessions < −20 bps: 264 · < −50 bps: 125 · > 0: 398

The mean is tail-dragged; the **median session still loses −1.79 bps**. KILL holds
on every lens (pooled, clustered, median, fill-only-pooled).

## The reversion exists but does not pay

`improvement | fill` by dip depth: 25 bps −5.52 · 50 bps −0.13 · **100 bps +5.52
(t +1.8, fill_rate 0.39)**. A genuine post-dip reversion appears only at the
deepest cut — but it is sub-significant, fires <40% of the time, and the no-fill
drag (−1.17) plus the catastrophic-continuation tail sink even the 100-bps blend
to −2.88. The short-term-reversal intuition is real; it is simply not tradeable
against the close baseline here.

## Bottom line

KILL is decisive and well-understood: post-50bps-dip continuation (concentrated
on high-vol days) swamps the faint reversion. touch_fraction 0.018 confirms this
is a genuine deep-through read, not an at-touch over-fill artifact. The buy leg
of the operator's final structure is closed with real data.
