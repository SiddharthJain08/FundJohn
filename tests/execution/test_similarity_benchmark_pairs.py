# tests/execution/test_similarity_benchmark_pairs.py
"""Spec D9: a pair containing a benchmark sleeve uses return-correlation only
once it has >= ALPHA_FULL_OBS overlapping observations; otherwise the normal blend."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
import execution.strategy_similarity as ss  # noqa: E402

OVERLAP = {'S_beta_spy': {'S_beta_spy': 1.0, 'S_timer': 0.05, 'S_pairs': 0.0},
           'S_timer':    {'S_beta_spy': 0.05, 'S_timer': 1.0, 'S_pairs': 0.2},
           'S_pairs':    {'S_beta_spy': 0.0, 'S_timer': 0.2, 'S_pairs': 1.0}}
RET = {'S_beta_spy': {'S_beta_spy': 1.0, 'S_timer': 0.7, 'S_pairs': -0.1},
       'S_timer':    {'S_beta_spy': 0.7, 'S_timer': 1.0, 'S_pairs': 0.3},
       'S_pairs':    {'S_beta_spy': -0.1, 'S_timer': 0.3, 'S_pairs': 1.0}}


def _nobs(n):
    return {(a, b): n for a in OVERLAP for b in OVERLAP if a != b}


def test_benchmark_pair_uses_return_corr_only_at_full_obs():
    out = ss.blend_similarity(OVERLAP, RET, _nobs(200), bench_ids={'S_beta_spy'})
    assert out['S_beta_spy']['S_timer'] == 0.7 and out['S_timer']['S_beta_spy'] == 0.7
    assert out['S_beta_spy']['S_pairs'] == -0.1
    # a non-benchmark pair keeps the blend: 0.4*0.2 + 0.6*0.3
    assert abs(out['S_timer']['S_pairs'] - (0.4 * 0.2 + 0.6 * 0.3)) < 1e-12


def test_thin_history_keeps_the_blend_for_benchmark_pairs():
    out = ss.blend_similarity(OVERLAP, RET, _nobs(30), bench_ids={'S_beta_spy'})
    al = ss.adaptive_alpha(30)
    assert abs(out['S_beta_spy']['S_timer'] - ((1 - al) * 0.05 + al * 0.7)) < 1e-12


def test_no_bench_ids_is_byte_identical():
    a = ss.blend_similarity(OVERLAP, RET, _nobs(200))
    b = ss.blend_similarity(OVERLAP, RET, _nobs(200), bench_ids=set())
    assert a == b


def test_bench_ids_for_rebuild_gated_on_flag(monkeypatch):
    # B3 (final fix wave, 2026-08-29): rebuild()/shadow_report() must not
    # pass real bench_ids into the D9 similarity rule until rule C itself is
    # live (OPENCLAW_BENCH_RELATIVE_SIZING=1) -- otherwise the similarity
    # exemption goes live ahead of the hurdle flip.
    monkeypatch.delenv('OPENCLAW_BENCH_RELATIVE_SIZING', raising=False)
    assert ss._bench_ids_for_rebuild() == set()

    monkeypatch.setattr('execution.benchmark_sleeve.load_benchmark_sleeve_ids',
                        lambda conn=None: {'S_beta_spy'})
    monkeypatch.setenv('OPENCLAW_BENCH_RELATIVE_SIZING', '1')
    assert ss._bench_ids_for_rebuild() == {'S_beta_spy'}
