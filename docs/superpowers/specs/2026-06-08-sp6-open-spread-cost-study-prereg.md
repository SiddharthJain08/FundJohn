# SP-6 — Open-Window Spread-Cost Study (PRE-REGISTERED)

Date: 2026-06-08. Status: **PRE-REGISTERED — windows / sampling / verdict
thresholds locked before any quote is pulled.** The discriminating measurement
for the longs+shorts open-exit structure: gross is closed (Probe ①), the only
open variable is the **incremental NBBO half-spread** crossed by exiting at the
open instead of the close. net = gross_edge − incremental_half_spread.

## 0. Decision quantity

Open-exit per-leg edge (gross, from Probe ①, positive = favorable to open-exit):
- **long gross edge = −E[intraday_return] = +0.098 bps** (≈ 0; null).
- **short gross edge = +E[intraday_return] = +1.940 bps** (near-sig, favorable).

You cross a half-spread on BOTH the open exit and the close-baseline exit, so
the marginal cost of moving to the open is the **incremental half-spread**:
`incr = open_half_spread − close_half_spread`. Then:
- `long_net = +0.098 − incr`
- `short_net = +1.940 − incr`

## 1. Population & sampling (locked)

- Source: `max_hold` LONG and SHORT exits (primary_window runs), same family
  as Probe ①, restricted to **exit_date ∈ [2025-06-01, 2026-05-31]** (recent
  12 months — spreads are microstructure-current, unlike the 10y drift signal).
  Recent counts: LONG 6,577 / SHORT 5,318 distinct (ticker,date); 408 names.
- Sample **min(2000, N) per direction**, deterministic: sort the distinct
  (ticker, exit_date) pairs lexicographically, take an even stride
  `step = ceil(N/2000)`, pick indices 0, step, 2·step, …. No RNG (reproducible).
- Each sampled (ticker, exit_date) is one event; the per-event spread is the
  unit of analysis (equal-weight by exit event — position/NAV weighting is a
  documented later refinement, not needed if the result is decisive).

## 2. Windows & measurement (locked)

- DST-aware via `zoneinfo.ZoneInfo("America/New_York")` per exit_date.
- **OPEN window**: 09:31:00–09:32:00 ET (≥9:31 per the operational guard —
  skips the opening-auction minute).
- **CLOSE window**: 15:55:00–15:56:00 ET (the Phase A ~3:55 baseline).
- Pull SIP NBBO via `alpaca data quotes --symbol T --start … --end …` (feed
  default sip; paced + 429-retry; counts-only progress, no spread values).
- Per quote: `half_spread_bps = ((ap − bp)/2) / ((ap + bp)/2) · 1e4`. Drop
  crossed/locked/non-positive quotes (`ap ≤ bp` or `ap≤0` or `bp≤0`).
- Per (ticker, exit_date, window): **median** half_spread over the **first
  K=20 VALID quotes at/after the window-start time** (pull `--limit 50`; the
  09:31:00 / 15:55:00 start with the +1min end as an upper bound). A FIXED
  COUNT (not "all quotes in 60s") is required for comparability: `--limit 1000`
  over a full minute truncates the two windows to *different physical
  durations* (liquid open ~19s vs close ~1.3s) because quote density differs,
  biasing `incr` by ~the size of the verdict gates. Same K both windows
  measures "the spread right at submit time" comparably and avoids pagination.
  Require **≥3 valid quotes** (after dropping crossed/locked), else VOID.
  [AMENDMENT 2026-06-08, pre-pull: was "median over the window's quotes";
  corrected after quality review found the limit-1000 time-truncation bias.]
- Keep only events with BOTH windows valid → `incr = open − close`.

## 3. Pre-committed readout & verdict

- Report per direction: n_events, and the distribution (mean, median,
  p25/p75/p90) of open_half_spread, close_half_spread, and incr.
- `long_net = 0.098 − incr`, `short_net = 1.940 − incr`, computed at BOTH the
  population **mean** and **median** incr (of the respective direction's
  population).
- **VERDICT:**
  - **PARK** iff short_net ≤ 0 at BOTH mean and median incr (long is already
    ≤0 by construction since its gross is ~0 and incr>0). The open spread eats
    the only favorable leg → close-exit stands; channel closed with real cost.
  - **SHIP-SHORTS-CANDIDATE** iff short_net > +0.5 bps at the median incr (a
    shorts-only open-exit could pay after spread) → triggers a separate
    live-lane design decision (NOT longs — long_net ≤ 0).
  - **MARGINAL** otherwise (0 < short_net ≤ +0.5 at median) → operator call;
    likely not worth the 3-seam live build for sub-bp net.
- INVALID-DATA iff < 300 valid events in either direction.

## 4. No-peek & scope

Mechanical; progress prints counts only; the verdict block is the first look
at any spread number (the AAPL/MTZ feasibility peeks are acknowledged anchors,
not the study). Read-only (no master-data writes, no live code). Push / any
live change remain separate operator approvals. If PARK, the open-exit
structure is closed and the live-lane build (3 seams) is never undertaken.
