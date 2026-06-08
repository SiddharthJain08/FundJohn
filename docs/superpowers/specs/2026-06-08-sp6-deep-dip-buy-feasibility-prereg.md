# SP-6 — Deep-Dip Buy-Leg Feasibility (PRE-REGISTERED)

Date: 2026-06-08. Status: **PRE-REGISTERED — rule / windows / verdict locked
before any number is computed.** This isolates the ONE genuinely-measurable,
genuinely-untested leg of the operator's "final structure":

> BUY signals: limit priced **0.5% (50 bps) below the open**, resting from the
> open until **1:30 PM ET**; if unfilled, **market between 1:30–3:00 PM ET**.

## 0. Why this leg is different (and why bars CAN measure it)

The companion sell leg ("limit at the early high, post above market") is an
**at-touch passive order** — bars over-fill it (the high is one print, queue
position is everything), so it is KILL-only and already composed from the
passive-window study (`sell_naive +4.75bps → sell_oracle +87bps`, live-shadow
only). This buy leg is **NOT at-touch**: it is a **deep-through limit 50 bps
below the open**. When a name falls 50 bps and trades *through* the resting bid,
the fill is genuine — the market crosses your level, so queue position barely
matters. The over-fill artifact is confined to the thin *touch-and-bounce*
boundary (low ≈ limit), which we measure and report.

The decision question is therefore **bar-measurable and untested** by every
prior study (atlas = *unconditional* drift; spread study = quoted spread;
passive study = 2/5/8 bps offsets, not 50 bps):

> Of the buy signals you'd take anyway, given the name dipped 50 bps below its
> open, does buying at that dip beat buying at the close (Phase A baseline)?

The sign is genuinely uncertain a priori. "Buying into weakness = adverse
selection (it keeps falling)" is an **assertion**; short-term-reversal evidence
points the other way (intraday dips partially revert). We measure, not assert.

## 1. Data & population (locked)

- Source: `data/cache/min_bars_hist/` (813 sessions 2023–2026, frozen 505-name
  universe `analysis/bflow_phase1b_hist/universe_505.txt`). Minute-indexed
  (0 = 09:30); columns o/h/l/c/v/vw. Read-only on the frozen cache.
- Unit of analysis = one (ticker, session). This is an **execution-timing**
  study over the same liquid universe; it is deliberately signal-agnostic (it
  asks "if you had to BUY this name today, does the dip-limit beat the close",
  which is exactly the buy-leg question for whatever the strategies signal).
- **open proxy** = `o` at minute 0 (the realized open). Live would use a
  pre-market estimate of the open; using the realized open is a documented small
  optimism (the dip is measured *from* the open intraday, so the reference is
  correct; only the live-estimation error is omitted).
- Eligibility (else VOID, not counted): minute-0 `o` present AND
  `close_benchmark` present AND the 1:30–3:00 PM fallback window non-empty.
- **n ≥ 600 eligible sessions** required for any verdict, else INVALID-DATA.

## 2. Rule & measurement (locked)

Constants (LOCKED): `DIP_BPS = 50.0` (primary; operator's 0.5%),
`FILL_WIN = (1, 240)` (09:31 → 13:30; the ≥9:31 operational guard — a
limit-on-open 50 bps below the open cannot fill in the opening auction, which
clears at the open, so excluding minute 0 loses no fill by construction),
`PM_WIN = (240, 330)` (13:30 → 15:00 ET market fallback), `HS_C = 1.0` bps
(close + PM marketable half-spread; PM is liquid so PM-spread ≈ close-spread),
`TOUCH_BPS = 2.0` (touch-boundary band for the over-fill diagnostic),
`DUMP_MIN = 385`, `MIN_SESSIONS = 600`.

Per (ticker, session) at `DIP_BPS`:
- `open0 = o@minute0`; `limit = open0 · (1 − DIP_BPS/1e4)`.
- `low_fill = min(l, FILL_WIN)`. **FILL** iff `low_fill ≤ limit` → realized buy
  price = `limit` (resting limit fills AT the limit, not at the low).
- If no fill: `pm_vwap = mean(vw, PM_WIN)`; realized buy price =
  `pm_vwap · (1 + HS_C/1e4)` (marketable).
- `baseline = close_benchmark · (1 + HS_C/1e4)` (Phase A close-exit cost).
- `improvement_bps = (baseline − realized_buy_price) / close_benchmark · 1e4`
  (positive = bought cheaper than the close baseline).
- **touch-boundary flag** (fills only): `True` iff
  `low_fill ≥ limit · (1 − TOUCH_BPS/1e4)` (low barely reached the limit →
  over-fill-ambiguous); else clear-through (trustworthy).

Sensitivity ladder (diagnostic, verdict reads off 50): also compute at
`DIP_BPS ∈ {25, 50, 100}` to show the fill-rate ↔ edge frontier.

Context row (diagnostic): unconditional `(close − open0)/close · 1e4`
(open-to-close drift; expected ≈ 0 per the atlas — contextualizes the
conditional result).

## 3. Statistic (locked)

Session-clustered (matches `passive_window_feasibility.clustered_t`):
per-session mean → across-session mean → `t = mean / (sd ddof=1 / √n_sessions)`.
Report, at each DIP_BPS:
- `improvement_bps` blended (mean, t, n_sessions) — the decision number.
- `fill_rate` (overall) and `E[improvement | fill]`, `E[improvement | no-fill]`
  (each session-clustered) — decomposes the blend.
- `touch_fraction` = (fills with touch-boundary flag) / (all fills) — the
  over-fill-trust diagnostic.

## 4. Pre-committed verdict (at DIP_BPS = 50, the operator's number)

- **INVALID-DATA** iff < 600 eligible sessions.
- **KILL** iff blended `improvement_bps ≤ 0` (mean, session-clustered). Even
  with the realized open as a free reference, the deep-dip buy loses to the
  close — continuation/adverse-selection after a 50 bps dip, and/or too-rare
  fills falling back to ~baseline, dominate. The buy leg of the structure is
  closed **with real data** (a strictly stronger result than asserting it).
- **MEASURED-POSITIVE** iff blended `improvement_bps > 0` AND `t ≥ 3` AND
  `touch_fraction < 0.5` (fills predominantly clear-through → bars trustworthy).
  A genuine, **bar-measurable** conditional-reversion edge that — unlike the
  at-touch passive sell — is NOT inherently live-shadow-only. Warrants a
  realizability follow-up (PM-spread sensitivity at HS_C∈{1,3}; the touch-band
  discount; live-estimation error on the open). NOT an automatic go-live.
- **MARGINAL** otherwise (`0 < improvement_bps` but `t < 3` OR
  `touch_fraction ≥ 0.5`) → operator call; sub-significant or over-fill-tainted.

## 5. No-peek & scope

Mechanical; progress prints counts only; the verdict block is the first look at
any improvement number. Read-only on the frozen cache (no master-data writes, no
live code). MEASURED-POSITIVE authorizes a realizability follow-up, NOT a
go-live. KILL closes the buy leg of the final structure with real data. Any push
to origin / any live change remain separate operator approvals.
