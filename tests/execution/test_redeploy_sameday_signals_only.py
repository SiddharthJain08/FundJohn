"""Intraday redeploy executes the FULL sequence in BOTH signal-target lanes.

OPERATOR RULING 2026-08-10: a regime transition SHOULD submit positions when
signals are determined and cross the conviction threshold — full redeploy
execution (signals → news-gate → handoff,trade,alpaca,reconcile) is intended
behaviour in the same-day lane too. This file previously pinned a short-lived
signals-only gate for the same-day lane (introduced and reverted the same
morning); it now pins the ruling so the gate does not silently reappear.
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


def _assert_full_sequence(calls, rc):
    assert rc == 0
    assert calls['spawn'] == [rd._REDEPLOY_STEPS_PRE_GATE,
                              rd._REDEPLOY_STEPS_POST_GATE], \
        'redeploy must run the full pre-gate → post-gate sequence, got %r' \
        % calls['spawn']
    assert len(calls['gate']) == 1, 'news gate runs between the two legs'


def test_sameday_lane_runs_full_redeploy(monkeypatch):
    monkeypatch.setenv('OPENCLAW_SAMEDAY_SIGNAL_TARGET', '1')
    calls = _harness(monkeypatch)
    rc = rd._run_redeploy('INTRADAY_HMM_LOW_VOL_TRANSITIONING', '2026-08-10',
                          dry_run=False)
    _assert_full_sequence(calls, rc)


def test_eod_lane_runs_full_redeploy(monkeypatch):
    monkeypatch.setenv('OPENCLAW_SAMEDAY_SIGNAL_TARGET', '0')
    calls = _harness(monkeypatch)
    rc = rd._run_redeploy('INTRADAY_HMM_LOW_VOL_TRANSITIONING', '2026-08-10',
                          dry_run=False)
    _assert_full_sequence(calls, rc)


def test_pre_gate_failure_short_circuits_both_lanes(monkeypatch):
    monkeypatch.setenv('OPENCLAW_SAMEDAY_SIGNAL_TARGET', '1')
    calls = _harness(monkeypatch, rcs=(3,))
    rc = rd._run_redeploy('INTRADAY_HMM_LOW_VOL_TRANSITIONING', '2026-08-10',
                          dry_run=False)
    assert rc == 3
    assert calls['spawn'] == [rd._REDEPLOY_STEPS_PRE_GATE]
    assert calls['gate'] == []
