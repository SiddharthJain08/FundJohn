"""Shared finalize-and-persist helpers for the sized-orders handoff.

Used by the live sizer (regime_blended_sizer_live). Encapsulates the
idempotency guard against `alpaca_submissions`, the prefiltered-into-vetoed
fold, the on-disk handoff write, and the veto_log append.
"""
import json
import os
from datetime import date, datetime, timezone


def finalize_sized_payload(run_date: str, payload: dict, source: str) -> bool:
    """Persist a sized handoff for the cycle.

    Mutates `payload` to add `source`/`generated_at`/`sized_at`/`cycle_date`
    defaults, folds the structured handoff's `prefiltered[]` into `vetoed[]`,
    runs the alpaca_submissions idempotency guard, writes the sized handoff,
    and appends veto_log rows. Returns True on success, False if the
    idempotency guard refused the write.
    """
    from execution.handoff import read_handoff as _read_handoff
    from execution.handoff import write_handoff as _write_handoff

    payload.setdefault('cycle_date', run_date)
    payload['source']       = source
    payload['generated_at'] = date.today().isoformat()
    # Wave stamp: identifies THIS sizing pass. The executor derives a
    # _w<HHMMSS> tag from it so per-order idempotency (already_executed +
    # client_order_id) is scoped to the wave, not the whole run_date —
    # required for the same-day afternoon top-up (2026-08-11) to submit
    # fresh delta orders after a morning redeploy wave already traded.
    payload['sized_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    # Fold any prefiltered signals from the structured handoff into the
    # sized handoff's `vetoed` list so send_report's digest reflects
    # everything that didn't make it to Alpaca — sizer vetoes AND the
    # prefilter drops. Deduplicates by (ticker, strategy_id).
    try:
        structured = _read_handoff(run_date, 'structured') or {}
        prefiltered = structured.get('prefiltered') or []
        if prefiltered:
            existing = payload.setdefault('vetoed', [])
            seen = {(v.get('ticker'), v.get('strategy_id')) for v in existing}
            for p in prefiltered:
                key = (p.get('ticker'), p.get('strategy_id'))
                if key not in seen:
                    existing.append(p)
                    seen.add(key)
            print(f'[sized_handoff] folded {len(prefiltered)} prefiltered signals into vetoed list')
    except Exception as e:
        print(f'[sized_handoff] prefilter-fold skipped: {e}')

    # Idempotency guard: if Alpaca has already received this cycle's
    # orders, refuse to overwrite the sized handoff. Override via
    # OPENCLAW_FORCE_RESIZE=1 when intentionally rerunning.
    if os.environ.get('OPENCLAW_FORCE_RESIZE') != '1':
        postgres_uri = os.environ.get('POSTGRES_URI', '')
        if postgres_uri:
            try:
                import psycopg2
                _conn = psycopg2.connect(postgres_uri)
                _cur  = _conn.cursor()
                # Check by run_date (the cycle this sized handoff belongs
                # to) — NOT submitted_at::date. submitted_at can be later
                # than run_date (e.g. a recovery script for run_date=Mon
                # that fires Tue morning), and the wrong key causes today's
                # legitimate cycle to refuse-overwrite because yesterday's
                # straggler-recovery happened to land today.
                _cur.execute(
                    "SELECT COUNT(*) FROM alpaca_submissions WHERE run_date = %s",
                    (run_date,),
                )
                already = _cur.fetchone()[0] or 0
                _conn.close()
                # PURE-FLATTEN exemption: a payload that is nothing but
                # __close_orphan__ liquidations (the zero-conviction flatten)
                # must NOT be discarded by the refuse-to-overwrite below. It is
                # safe to (re)write: the closes were sized from the LIVE broker
                # book moments ago, and the executor's per-order
                # already_executed() still dedups each close individually — a
                # pure flatten cannot double-OPEN anything. Without this, the
                # same-day lane's 15:00 flatten dies here whenever the 10:00
                # base cycle submitted anything: 2026-08-07 the sizer logged
                # "$68,749 gross liquidated" for 47 closes and $0 reached the
                # broker — the executor re-read the MORNING handoff instead.
                _orders = payload.get('orders') or []
                _pure_flatten = bool(_orders) and all(
                    (o.get('strategy_id') or '') == '__close_orphan__'
                    for o in _orders)
                # Same-day top-up lane (operator directive 2026-08-11): the
                # 15:00 ET EOD compute is the AUTHORITATIVE same-day sizing.
                # When the morning intraday redeploy already traded, this
                # lane's payload is a fresh set of deltas vs the LIVE broker
                # book (which already includes the morning fills) — writing it
                # is exactly the intended top-up/re-true into the close. The
                # executor's wave-scoped dedup (sized_at → _w<HHMMSS> coid
                # tag) keeps re-runs of THIS wave idempotent while letting its
                # orders through past the morning wave's submissions.
                # 2026-08-10 incident: the afternoon compute targeted $43.2k
                # gross vs the morning's $23.7k and the whole re-size was
                # silently discarded here — realized book stayed at 45% of the
                # authoritative target. Kill-switch: OPENCLAW_SAMEDAY_TOPUP=0.
                # Deliberately NOT enabled for the intraday-redeploy lane
                # (every 5-min HMM transition re-truing the book) and not for
                # empty payloads (nothing to execute — keep the earlier
                # handoff for send_report / d-1 context).
                _topup_lane = (
                    os.environ.get('OPENCLAW_EOD_RECONCILE') == '1'
                    and os.environ.get('OPENCLAW_INTRADAY_REDEPLOY') != '1'
                    and os.environ.get('OPENCLAW_SAMEDAY_TOPUP', '1') != '0')
                if already > 0 and _pure_flatten:
                    print(f'[sized_handoff] {already} Alpaca submission(s) already exist for '
                          f'{run_date}, but this payload is a PURE FLATTEN '
                          f'({len(_orders)} __close_orphan__ close(s), no opens) — '
                          f'writing it over the earlier sized handoff so the '
                          f'liquidation actually reaches the executor.')
                elif already > 0 and _topup_lane and _orders:
                    print(f'[sized_handoff] {already} Alpaca submission(s) already exist for '
                          f'{run_date}, but this is the authoritative same-day EOD compute — '
                          f'RE-TRUE: writing the fresh sized handoff ({len(_orders)} order(s), '
                          f'deltas vs the live broker book) over the earlier wave. Executor '
                          f'dedup is wave-scoped via sized_at={payload["sized_at"]}. '
                          f'Disable with OPENCLAW_SAMEDAY_TOPUP=0.')
                elif already > 0:
                    # An intraday regime-redeploy (or a same-cycle re-run) has
                    # already submitted this run_date's orders. Do NOT abort:
                    # the old `return False` surfaced as the trade step's rc=3,
                    # which killed the 3:55pm EOD into-close fill's
                    # reconcile/report/health on EVERY redeploy day — including
                    # the EOD report on exactly the days a regime moved
                    # (ERR-20260612-001, observed 06-09 + 06-11).
                    #
                    # Instead, leave the existing sized handoff intact
                    # (send_report and trade_handoff_builder's d-1 context both
                    # read it) and return True so the cycle proceeds to
                    # alpaca -> reconcile -> report -> health. The executor's
                    # per-order already_executed() idempotency then skips every
                    # filled order, so nothing is re-submitted. (It will still
                    # retry any order that FAILED earlier in the day — its
                    # existing per-order retry semantics.) Override with
                    # OPENCLAW_FORCE_RESIZE=1 to deliberately re-size.
                    # (Reached by: the intraday-redeploy lane, the top-up
                    # kill-switch, empty-orders payloads, and non-same-day
                    # deployments without OPENCLAW_EOD_RECONCILE.)
                    print(f'[sized_handoff] {already} Alpaca submission(s) already exist for '
                          f'{run_date} (intraday redeploy or same-cycle re-run already executed '
                          f"today's target) — leaving the existing sized handoff intact and "
                          f'proceeding (no re-size; executor idempotently skips filled orders). '
                          f'Set OPENCLAW_FORCE_RESIZE=1 to override.')
                    return True
            except Exception as _e:
                print(f'[sized_handoff] alpaca_submissions check skipped: {_e}')

    if os.environ.get('OPENCLAW_TRADE_AGENT_DRY_RUN') == '1':
        n_orders = len(payload.get('orders', []))
        n_vetoed = len(payload.get('vetoed', []))
        print(f'[sized_handoff] DRY-RUN: would have written sized handoff — '
              f'{n_orders} orders, {n_vetoed} vetoed (skipping write + veto_log)')
        print('--- DRY-RUN sized payload ---')
        print(json.dumps(payload, indent=2, default=str)[:8000])
        print('--- end ---')
        return True

    _write_handoff(run_date, 'sized', payload)
    print(f'[sized_handoff] Sized handoff written — {len(payload.get("orders", []))} orders, '
          f'{len(payload.get("vetoed", []))} vetoed.')
    write_veto_log_rows(run_date, payload.get('vetoed') or [])
    return True


def write_veto_log_rows(run_date: str, vetoed: list[dict]) -> None:
    if not vetoed:
        return
    postgres_uri = os.environ.get('POSTGRES_URI', '')
    if not postgres_uri:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(postgres_uri)
        cur  = conn.cursor()
        for v in vetoed:
            reason = v.get('reason') or 'unknown'
            ticker = v.get('ticker')
            strat  = v.get('strategy_id')
            ev     = v.get('ev')
            kelly  = v.get('kelly_final') or v.get('kelly')
            cur.execute(
                '''INSERT INTO veto_log
                     (run_date, strategy_id, ticker, veto_reason, ev, kelly)
                   VALUES (%s, %s, %s, %s, %s, %s)''',
                (run_date, strat, ticker, reason, ev, kelly),
            )
        conn.commit()
        conn.close()
        print(f'[sized_handoff] veto_log — {len(vetoed)} row(s) appended')
    except Exception as e:
        print(f'[sized_handoff] veto_log write failed: {e}')
