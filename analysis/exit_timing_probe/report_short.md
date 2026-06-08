# Probe ① — SHORT-exit diagnostic (companion to the locked long probe)

**Status:** DIAGNOSTIC (operator-requested), not a pre-registered gate. Same
machinery as the locked long probe; `direction='short'` max_hold exits.

**Sign convention:** a short covers by BUYING, so open-exit helps a short iff
the session is POSITIVE (open cheaper than close). Short open-exit edge =
**+E[intraday_return]** — the mirror of the long edge (−E[intraday_return]).

## Headline (day-clustered)

- PRIMARY (max_hold-short, 32,025 rows / 2,437 day-clusters):
  intraday_return mean **+1.9402 bps**, t **+2.0213** → short open-exit edge
  = **+1.94 bps, t +2.02**.
- SECONDARY (equity universe): +0.6647 bps, t +0.38.
- M2 relative (short-exit names − same-day universe): +1.9792 bps, t +1.35.

## By regime (PRIMARY short)

| regime | mean bps | t | n_days |
|---|---|---|---|
| CRISIS | +4.793 | +0.776 | 127 |
| HIGH_VOL | −2.213 | −0.840 | 378 |
| LOW_VOL | +2.076 | +1.734 | 884 |
| TRANSITIONING | +2.996 | +1.894 | 1047 |

(Regime pattern is the MIRROR of longs: shorts favorable in calm/transitioning,
adverse in HIGH_VOL; longs were favorable in HIGH_VOL/CRISIS. All insignificant.)

## Recent half-years (PRIMARY short)

2024H2 −4.384 (t−1.04) · 2025H1 +0.416 (t+0.08) · 2025H2 −0.162 (t−0.03) ·
2026H1 +9.412 (t+1.51). Noisy; no recent significance.

## Read

- **By the strict t≥3 bar, shorts are NULL on gross** — same as longs.
- **But the short point estimate (+1.94 bps, t+2.02) is favorable and far
  larger than the long (~0, t−0.10).** The a-priori theory — "shorts get hurt
  by open-exit because they forfeit a favorable intraday session" — is NOT
  supported by the data. The opposite shows up.
- **Why:** selection. `max_hold`-short exits are shorts that DIDN'T hit target
  and aged out — disproportionately names drifting UP against the short
  (the short "isn't working"). Such names keep rising intraday → covering at
  the open (before the rise) is cheaper. This selection is REPRESENTATIVE of
  the live trigger (signals drop a short that isn't paying), not a backtest
  artifact.
- **Net still faces the open spread.** +1.94 bps gross has more cushion than
  the long's ~0, but it is still sub-significance and small; the wide-spread
  open cover erodes it. Forward live fills remain the only net arbiter, and a
  ~2 bp gross is still hard to resolve there.

## Implication for the structure

The longs-only design was justified on a theory the data contradicts. On the
gross numbers, **shorts have the stronger (favorable) open-exit signal**, so:
- If longs move to open (operator's decision) on thin/variance grounds, the
  SAME-OR-STRONGER case exists for shorts → "all signal-driven exits at open"
  is more data-consistent than "longs-only."
- Keeping shorts at close is now a CONSERVATISM choice (wider open spread on
  covers, avoid acting on a t+2.0 selection effect), not a theory-backed one.
- Neither clears a strict gross bar; net is spread-dependent for both.
