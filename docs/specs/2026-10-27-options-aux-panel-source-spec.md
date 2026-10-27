# Options aux from the surface master (panel source) — spec 2026-10-27

**Status:** LANDED behind flags, both OFF. Operator flip via the existing
`scripts/options_surface_flip_after_shadow.sh` gate (G1/G2/G3) — this spec
adds a SOURCE for the v2 dict, not a new flip.

## Problem
The live signals step derives ~20 per-ticker options fields by re-aggregating
a 14-day window of raw `options_eod` contract rows on every run. Under the
monthly layout (`OPENCLAW_OPTIONS_MONTHLY=1`, 10-21) and in LOW_VOL (where
`S_options_flow_confirmed_momentum` pulls the `options` kind into the aux plan)
that read peaked 4.3 GB (10-26) and 5.2 GB (10-27, rc=137 at the 4500M step
cap). `b85fe321` cut the read to 2.3 GB by streaming row groups; the raw
re-aggregation itself is still the dominant cost of the step, and the engine
docstring has owed its removal since 2026-07-29: *"serve the live options aux
from the pre-aggregated panel + intraday overlay instead of re-aggregating raw
chains on every run — same math, ~225k rows instead of 18M, and it puts live
and backtest on ONE source."*

## What already exists (grounding)
- `data/master/options_surface.parquet` — the surface master: 297k rows, 39
  columns, one row per (ticker, session) written by
  `scripts/build_options_surface.py` via `strategies.options_surface.
  features_for_day(prepare_chain(rows, day), spot, day)`; refreshed daily at
  11:15Z (`refresh_options_aggregates`, window `--start T-8 --end T-1`); max
  date = T-1 before the 15:00 ET compute.
- `src/execution/options_aux_v2.py` — the v2 live dict (spec 2026-09-04 A.7):
  `features_for_day` on the raw chain at the chain date, master precedence for
  `iv30`/`pc_ratio`, `series_features` over the ticker's prior master rows +
  realized vol from the price window, CBOE OI features, `earnings_dte`,
  `surface_date`, `spot_date`. Served when `OPENCLAW_OPTIONS_SURFACE=1`,
  otherwise a `[options_surface] shadow …` line — skipped on the 15:00 ET
  compute (`OPENCLAW_SIGNALS_SKIP_SHADOW_OPTIONS=1`, M2 2026-09-28) because the
  raw build costs ~200 s / 0.8 GB there. The flip gate has read NOT_YET since
  09-04 (G2 dirty, G3 lagging) and has had no G2 lines to read from the
  same-day compute at all.
- Tier-1 overlay (`OPENCLAW_INTRADAY_AUX=1`): `data/derived/intraday/<T>/
  options_raw.parquet`, today's raw rows for the acting set (462k rows on
  10-27), ≤ 6 h old, spliced by `_inject_intraday_options` so the chain date
  is T at 15:00 ET.

## Design
New module `src/execution/options_aux_panel.py`, `build(universe, today,
master_dir, px_window, earnings_upcoming, overlay)` → `{ticker: {…v2 keys…}}`:

| ticker state | per-day feature row | then |
|---|---|---|
| overlay rows for the ticker (fresh) | `features_for_day(prepare_chain(overlay rows, T), spot, T)` — the same call v2 makes on the spliced rows; a master row dated T (none at 15:00 ET) takes precedence for `iv30`/`pc_ratio` exactly as in v2 | `series_features` over master rows `< T`, CBOE OI, `earnings_dte`, `surface_date=T`, `spot_date` |
| no overlay rows | the latest master row `<= T` **is** the row (same function wrote it from the same EOD rows); `near_iv = iv30` as v2 sets under master precedence; `last_price` = last close ≤ that date | same, with history `<` that date |
| neither | ticker absent (as in v2) | |

Inputs are one filtered read of the master (`date ∈ [T−400 d, T]`,
`ticker ∈ universe`) plus the overlay. **The raw options window is never
opened.**

Flags (read at call time):

| `OPENCLAW_OPTIONS_SURFACE` | `OPENCLAW_OPTIONS_SURFACE_SOURCE` | served | shadow line |
|---|---|---|---|
| unset/0 | unset/`raw` (default) | legacy dict | v2 raw build vs legacy (skipped on same-day compute) — unchanged |
| unset/0 | `panel` | legacy dict (raw read unchanged) | **panel build vs legacy, every run incl. 15:00 ET** |
| 1 | `raw` | v2 raw build | — (unchanged) |
| 1 | `panel` | **panel build; raw window skipped** (`Options loaded from the surface master: N tickers`) | panel vs `{}` (coverage line for G2) |

Engine wiring: `_options_served_from_panel()` gates a new branch ahead of the
raw `options` block in `load_aux_data` (`if … elif …`, no re-indentation of the
raw path); `_serve_options_from_panel` derives the price window and the
upcoming-earnings split exactly as the raw block does; `_apply_options_surface`
takes the panel build as `new` when the source is `panel`.

## Parity contract (tested)
`tests/execution/test_options_aux_panel.py`: on the 2026-09-03 fixture chain,
with a master row for that day written by `features_for_day`, the panel build
equals `options_aux_v2.build` **key for key** (master-row path), and equals it
again when the same chain is handed in as the overlay (overlay path).
Engine tests: the live branch never calls `_load_options_window`; the shadow
branch uses the panel build and ignores the same-day skip; both flags are
required for the served path.

## Rollout
1. Now: `OPENCLAW_OPTIONS_SURFACE_SOURCE=panel` with `OPENCLAW_OPTIONS_SURFACE`
   still 0 → shadow lines return on the 15:00 ET compute at panel cost
   (seconds, < 0.5 GB), feeding the flip gate's G2 series. Nothing served
   changes. **Not set tonight** — operator decision (it changes which build the
   gate's G2 criterion measures: panel-vs-legacy instead of raw-v2-vs-legacy;
   the two builds are equal by the parity contract, so the ratios are the same).
2. The existing flip (`options_surface_flip_after_shadow.sh --apply`, gated
   G1/G2/G3) sets `OPENCLAW_OPTIONS_SURFACE=1`; with source=panel the served
   dict is the panel build and the raw window read disappears from the step.
3. Rollback: source back to `raw` (or unset) — v2 behaviour byte-identical to
   before this spec; `b85fe321`'s streaming raw read remains the fallback.

## Not measured
Full-universe `build()` wall clock on the live store (per-ticker CBOE OI
lookup + `features_for_day` on overlay rows is the same loop v2 runs under its
240 s shadow budget). Five tickers: 1.6 s, 452 MB RSS including imports
(10-27 21:2xZ). First shadow line under source=panel is the measurement.
