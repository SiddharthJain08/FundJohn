"""Tests for Phase 2F mastermind_recalibration.

Detection/generation tests run in-process. DB integration tests verify
the SQL paths (FOR UPDATE, supersedes_id, status transitions) on the
live Postgres at $POSTGRES_URI; cleaned up via test-tagged rows.
"""
from __future__ import annotations
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from agent import mastermind_recalibration as mr  # noqa: E402


# ---------- detection (pure) ---------- #

def test_detect_bias_returns_empty_on_insufficient_buckets():
    rep = {'total_observations': 5, 'brier_score': None,
            'buckets': [{'range': '[0.0, 0.2]', 'count': 5,
                          'matched': 1, 'match_rate': 0.2}]}
    assert mr.detect_bias(report=rep) == []


def test_detect_bias_skips_clean_buckets():
    """match_rate near midpoint → not biased."""
    rep = {'buckets': [{'range': '[0.4, 0.6]', 'count': 20,
                         'matched': 10, 'match_rate': 0.5}]}
    assert mr.detect_bias(report=rep) == []


def test_detect_bias_flags_overconfident_bucket():
    """0.8-midpoint=0.9 vs match_rate=0.55 → delta=-0.35, overconfident."""
    rep = {'buckets': [{'range': '[0.8, 1.0]', 'count': 30,
                         'matched': 16, 'match_rate': 0.55}]}
    biases = mr.detect_bias(report=rep)
    assert len(biases) == 1
    assert biases[0]['direction'] == 'overconfident'
    assert biases[0]['delta'] < -0.15
    assert biases[0]['count'] == 30


def test_detect_bias_flags_underconfident_bucket():
    """0.2-midpoint=0.3 vs match_rate=0.7 → underconfident."""
    rep = {'buckets': [{'range': '[0.2, 0.4]', 'count': 25,
                         'matched': 18, 'match_rate': 0.72}]}
    biases = mr.detect_bias(report=rep)
    assert len(biases) == 1
    assert biases[0]['direction'] == 'underconfident'


def test_detect_bias_handles_multiple_buckets():
    rep = {'buckets': [
        {'range': '[0.0, 0.2]', 'count': 15, 'matched': 9, 'match_rate': 0.6},
        {'range': '[0.4, 0.6]', 'count': 20, 'matched': 10, 'match_rate': 0.5},
        {'range': '[0.8, 1.0]', 'count': 20, 'matched': 5, 'match_rate': 0.25},
    ]}
    biases = mr.detect_bias(report=rep)
    assert len(biases) == 2
    labels = {b['bucket'] for b in biases}
    assert labels == {'[0.0, 0.2]', '[0.8, 1.0]'}


def test_generate_addendum_overconfident_text():
    bias = {'bucket': '[0.8, 1.0]', 'count': 30, 'match_rate': 0.55,
            'midpoint': 0.9, 'delta': -0.35, 'direction': 'overconfident'}
    text = mr.generate_addendum(bias)
    assert '[0.8, 1.0]' in text
    assert '55.0%' in text
    assert 'Discount' in text


def test_generate_addendum_underconfident_text():
    bias = {'bucket': '[0.2, 0.4]', 'count': 25, 'match_rate': 0.72,
            'midpoint': 0.3, 'delta': 0.42, 'direction': 'underconfident'}
    text = mr.generate_addendum(bias)
    assert 'underconfident' in text


def test_bucket_midpoint_parsing():
    assert mr._bucket_midpoint('[0.0, 0.2]') == 0.1
    assert mr._bucket_midpoint('[0.8, 1.0]') == 0.9
    assert mr._bucket_midpoint('garbage') is None
    assert mr._bucket_midpoint('') is None


# ---------- DB integration (cleaned up) ---------- #

def _have_db():
    try:
        with mr._connect():
            pass
        return True
    except Exception:
        return False


pytestmark_db = pytest.mark.skipif(not _have_db(), reason='no Postgres available')


@pytestmark_db
def test_db_emit_auto_addenda_inserts_pending():
    """Synthetic biased report → emit_auto_addenda inserts pending row.
    Cleanup via rejecting after to satisfy 'no DELETE' invariant."""
    rep = {'buckets': [{'range': '[0.8, 1.0]', 'count': 30,
                         'matched': 16, 'match_rate': 0.55}]}
    result = mr.emit_auto_addenda(report=rep)
    assert result['status'] == 'OK'
    assert len(result['emitted']) == 1
    new_id = result['emitted'][0]['id']
    # Reject to leave a clean trail (no DELETE per CLAUDE.md)
    rej = mr.reject_addendum(new_id, decided_by='test', reason='cleanup')
    assert rej['status'] == 'OK'


@pytestmark_db
def test_db_emit_supersedes_prior_auto_for_same_bucket():
    rep1 = {'buckets': [{'range': '[0.6, 0.8]', 'count': 20,
                          'matched': 8, 'match_rate': 0.40}]}
    rep2 = {'buckets': [{'range': '[0.6, 0.8]', 'count': 25,
                          'matched': 9, 'match_rate': 0.36}]}
    r1 = mr.emit_auto_addenda(report=rep1)
    first_id = r1['emitted'][0]['id']
    r2 = mr.emit_auto_addenda(report=rep2)
    second_id = r2['emitted'][0]['id']
    assert r2['emitted'][0]['supersedes_id'] == first_id
    # Cleanup
    mr.reject_addendum(second_id, decided_by='test', reason='cleanup')
    # first_id is already 'superseded'


@pytestmark_db
def test_db_operator_addendum_is_immediately_active():
    new_id = mr.create_operator_addendum(
        text='Test operator addendum',
        rationale='unit test',
        decided_by='pytest')
    active = mr.get_active_addenda()
    ids = {a['id'] for a in active}
    assert new_id in ids
    # Transition out so test doesn't leave it 'active'
    mr.expire_addendum(new_id, decided_by='test', reason='cleanup')


@pytestmark_db
def test_db_approve_pending_flips_to_active_and_sets_valid_from():
    rep = {'buckets': [{'range': '[0.4, 0.6]', 'count': 20,
                         'matched': 17, 'match_rate': 0.85}]}
    r = mr.emit_auto_addenda(report=rep)
    new_id = r['emitted'][0]['id']
    ok = mr.approve_addendum(new_id, decided_by='pytest', reason='unit')
    assert ok['status'] == 'OK'
    active = mr.get_active_addenda()
    ids = {a['id'] for a in active}
    assert new_id in ids
    # Cleanup
    mr.expire_addendum(new_id, decided_by='test', reason='cleanup')


@pytestmark_db
def test_db_get_active_auto_expires_past_valid_until():
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    new_id = mr.create_operator_addendum(
        text='Expiry test', rationale='unit',
        decided_by='pytest', valid_until=past)
    active = mr.get_active_addenda()
    ids = {a['id'] for a in active}
    assert new_id not in ids   # auto-expired on read


@pytestmark_db
def test_db_illegal_transition_returns_error():
    new_id = mr.create_operator_addendum(
        text='Illegal-trans test', rationale='unit',
        decided_by='pytest')
    # Currently 'active'; approve is illegal
    bad = mr.approve_addendum(new_id, decided_by='pytest', reason='shouldfail')
    assert bad['status'] == 'ILLEGAL_TRANSITION'
    # Cleanup
    mr.expire_addendum(new_id, decided_by='test', reason='cleanup')


@pytestmark_db
def test_db_get_active_empty_when_none():
    """Smoke: with no active addenda inserted by this test, the function
    succeeds and returns a list (possibly empty, possibly populated by
    earlier tests but always a list)."""
    result = mr.get_active_addenda()
    assert isinstance(result, list)
