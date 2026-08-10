"""Same-day lane: the intraday redeploy is SIGNALS-ONLY.

Operator directive 2026-08-10 — the 15:00/15:55 same-day lane is the SOLE
execution wave. Motivating incident 2026-08-07: the 10:01 LOW_VOL→TRANSITIONING
redeploy opened 47 positions; the 15:00 compute found zero conviction over the
same book and flattened it the same afternoon (see
test_sized_handoff_guard for the layer that then swallowed the flatten).
A regime transition must still recompute signals (they join the 15:00 carried
pool) but must never submit orders. The legacy T+1 EOD-register lane keeps the
full redeploy sequence unchanged.
"""
import scripts.redeploy_pipeline as rd


def _harness(monkeypatch, rcs=(0, 0)):
    """Stub every external surface of _run_redeploy; record call order."""
    calls = {'spawn': [], 'gate': [], 'hook': []}
    it = iter(rcs)
    monkeypatch.setattr(rd, '_log_acting_ingest_preflight', lambda d: None)
    monkeypatch.setattr(
        rd, '_spawn_orchestrator',
        lambda reason, run_date, dry_run, steps=rd.REDEPLOY_STEPS:
        (calls['spawn'].append(steps), next(it))[1])
    monkeypatch.setattr(rd, '_run_intraday_news_gate',
                        lambda d, dr: calls['gate'].append(d))
    monkeypatch.setattr(rd, '_post_webhook',
                        lambda ch, msg: (calls['hook'].append((ch, msg)), True)[1])
    return calls


def test_sameday_lane_redeploy_is_signals_only(monkeypatch):
    monkeypatch.setenv('OPENCLAW_SAMEDAY_SIGNAL_TARGET', '1')
    calls = _harness(monkeypatch)
    rc = rd._run_redeploy('INTRADAY_HMM_LOW_VOL_TRANSITIONING', '2026-08-10',
                          dry_run=False)
    assert rc == 0
    assert calls['spawn'] == [rd._REDEPLOY_STEPS_PRE_GATE], \
        'same-day redeploy must run ONLY the signals step, got %r' % calls['spawn']
    assert calls['gate'] == [], 'news gate is part of the execution leg — skipped'
    assert any('15:55' in msg for _ch, msg in calls['hook']), \
        'the deferral must be announced on the intraday-regime webhook'


def test_eod_lane_keeps_full_redeploy(monkeypatch):
    # Explicit legacy T+1 lane: new flag 0 → eod_register semantics.
    monkeypatch.setenv('OPENCLAW_SAMEDAY_SIGNAL_TARGET', '0')
    calls = _harness(monkeypatch)
    rc = rd._run_redeploy('INTRADAY_HMM_LOW_VOL_TRANSITIONING', '2026-08-10',
                          dry_run=False)
    assert rc == 0
    assert calls['spawn'] == [rd._REDEPLOY_STEPS_PRE_GATE,
                              rd._REDEPLOY_STEPS_POST_GATE], \
        'EOD lane must keep the full pre-gate → post-gate sequence'
    assert len(calls['gate']) == 1, 'news gate runs between the two legs'


def test_pre_gate_failure_short_circuits_both_lanes(monkeypatch):
    monkeypatch.setenv('OPENCLAW_SAMEDAY_SIGNAL_TARGET', '1')
    calls = _harness(monkeypatch, rcs=(3,))
    rc = rd._run_redeploy('INTRADAY_HMM_LOW_VOL_TRANSITIONING', '2026-08-10',
                          dry_run=False)
    assert rc == 3
    assert calls['spawn'] == [rd._REDEPLOY_STEPS_PRE_GATE]
    assert calls['gate'] == [] and calls['hook'] == []
