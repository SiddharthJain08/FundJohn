"""Daily-bar multi-day first-touch replay engine (policy-agnostic).

Scores any Policy (ensemble or baseline) on a single underlying's daily OHLC
path, producing the realized per-trade return ``R`` (signed return-on-entry, in
price-fraction units), holding time ``tau`` (bars), exit kind, and filled
fraction. Task 7 (growth.py) converts ``R`` to sigma units via ``sigma_ret``.

Fill conventions (the load-bearing decisions; uniform across all policies):

  * **Entry is bar 0** (the signal close[t]); it is never evaluated. The walk
    starts at the bar AFTER entry (``bars.iloc[1]``) and runs to
    ``min(time_stop_bars, len(bars)-1)`` inclusive. The passed ``entry`` price
    is the anchor (NOT ``close[0]``).
  * **Direction-signed excursions.** For a bar, the favorable excursion is
    ``max(dir*(high-entry), dir*(low-entry))`` and the unfavorable is the min.
    Long (dir +1): high is favorable; short (dir -1): low is favorable.
  * **Stop-wins-on-tie.** Within a bar the stop is checked BEFORE any take. If
    the unfavorable excursion reaches ``-stop_dist`` the entire remaining
    position closes at ``-stop_dist`` that bar (any take reachable the same bar
    is skipped). This is the conservative convention (we cannot see intrabar
    order from daily OHLC).
  * **Gap-fill at the level.** A bar that jumps past a level fills AT the level
    (``+b_k`` for a take, ``-stop_dist`` for the stop), never at the bar
    extreme -- conservative on takes, fair on stops.
  * **Ordered partial takes book fraction-of-ORIGINAL.** Each take ``k`` fires
    once, when the bar's favorable excursion reaches ``b_k`` OR the bar index
    reaches the take's ``time_bars`` (whichever first). It books
    ``f = min(fraction_k, remaining)`` of the ORIGINAL unit position at
    excursion ``+b_k`` (or the bar close excursion if time-triggered).
    ``R`` is a plain fraction-weighted sum (NO compounding):
    ``R = sum_k f_k * excursion_k / entry - carry_drag``.
  * **Horizon mark.** Any remainder still open at the final bar is booked at
    that bar's close excursion.
  * **Carry drag** (shorts / non-zero arm only): ``R -= abs(carry_per_bar)*tau``
    where ``tau`` is the absolute bar index of the final close.

Returns ``dict(R, tau, exit_kind, frac_filled)`` where ``exit_kind`` is
``"stop"`` (stopped out), ``"take"`` (fully booked via takes), or ``"time"``
(remainder marked at the horizon).
"""


def _excursions(bar, entry, direction):
    """Return (favorable, unfavorable) signed excursions for a bar."""
    hi = direction * (float(bar["high"]) - entry)
    lo = direction * (float(bar["low"]) - entry)
    return (max(hi, lo), min(hi, lo))


def first_touch_multiday(policy, bars, entry, carry_per_bar=0.0):
    entry = float(entry)
    direction = int(policy["direction"])
    stop_dist = float(policy["stop_dist"])
    # each take fires once; preserve declared order, default time_bars -> inf
    takes = [dict(distance=float(t["distance"]), fraction=float(t["fraction"]),
                  time_bars=float(t.get("time_bars", float("inf"))), fired=False)
             for t in policy["takes"]]

    n = len(bars)
    last = min(int(policy["time_stop_bars"]), n - 1)

    remaining = 1.0      # fraction-of-original still open
    R = 0.0             # signed return-on-entry, plain fraction-weighted sum
    frac_filled = 0.0    # sum of fractions booked via takes (not the horizon mark)
    exit_kind = "time"
    tau = last           # absolute bar index of final close (default: horizon)

    for i in range(1, last + 1):
        bar = bars.iloc[i]
        fav, unfav = _excursions(bar, entry, direction)

        # 1) stop-wins-on-tie: stop checked first, closes entire remainder
        if unfav <= -stop_dist and remaining > 1e-12:
            R += remaining * (-stop_dist) / entry
            remaining = 0.0
            exit_kind = "stop"
            tau = i
            break

        # 2) ordered partial takes (price-or-time trigger), book frac-of-original
        for t in takes:
            if t["fired"] or remaining <= 1e-12:
                continue
            price_hit = fav >= t["distance"]
            time_hit = i >= t["time_bars"]
            if not (price_hit or time_hit):
                continue
            f = min(t["fraction"], remaining)
            if f <= 0.0:
                t["fired"] = True
                continue
            # price-triggered fills at the level (+b_k); time-triggered marks
            # at the bar close excursion (the only price we can observe then)
            exc = t["distance"] if price_hit else direction * (float(bar["close"]) - entry)
            R += f * exc / entry
            remaining -= f
            frac_filled += f
            t["fired"] = True
            tau = i

        if remaining <= 1e-12:
            exit_kind = "take"
            tau = i
            break

    # 3) horizon mark: book any remainder at the final bar's close excursion
    if remaining > 1e-12:
        bar = bars.iloc[last]
        exc = direction * (float(bar["close"]) - entry)
        R += remaining * exc / entry
        remaining = 0.0
        tau = last

    # 4) carry drag (shorts / non-zero arm): charged on bars held
    R -= abs(float(carry_per_bar)) * tau

    return dict(R=float(R), tau=int(tau), exit_kind=exit_kind,
                frac_filled=float(frac_filled))
