"""Secondary-line post-pass (cache-wide security typing). Fakes + a real-cache
test (skipped when the vendor profile cache is absent). No DB/HTTP."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from src.strategies import universe_default as ud
from src.strategies.universe_meta import (
    NON_COMMON_SECURITY_TYPES, SECONDARY_DENY_LIST, TickerMetadata,
    security_type_from_profile, security_types_from_profiles)


def _p(name, cik='111', adr=False, **kw):
    d = {'companyName': name, 'cik': cik, 'isEtf': False, 'isFund': False, 'isAdr': adr}
    d.update(kw)
    return d


def test_secondary_is_non_common_and_excluded_from_common_stock():
    assert 'secondary' in NON_COMMON_SECURITY_TYPES
    m = TickerMetadata.from_row({
        'symbol': 'X', 'asset_class': 'us_equity', 'exchange': 'NYSE', 'status': 'active',
        'tradable': True, 'shortable': True, 'fractionable': True, 'easy_to_borrow': True,
        'market_cap': 1e10, 'adv_usd_20d': 1e7, 'sector': None, 'industry': None,
        'options_eligible': False, 'in_sp500': True, 'in_r1000': True, 'in_r3000': True,
        'listed_date': None, 'delisted_date': None, 'security_type': 'secondary'})
    assert ud.common_stock(m, date(2026, 10, 1)) is False


def test_rule_hit_and_per_profile_function_unchanged():
    prof = {'NTRS': _p('Northern Trust Corporation'),
            'NTRSO': _p('Northern Trust Corporation')}
    assert security_type_from_profile(prof['NTRSO']) == 'stock'   # pure fn untouched
    t = security_types_from_profiles(prof)
    assert t == {'NTRS': 'stock', 'NTRSO': 'secondary'}


@pytest.mark.parametrize('suffix', ['W', 'WW', 'WS', 'U', 'P', 'AP', 'PA', 'L', 'M', 'N', 'O', 'Z', 'XL', 'PP'])
def test_suffix_forms_hit(suffix):
    prof = {'ABC': _p('Abc Inc'), 'ABC' + suffix: _p('Abc Inc')}
    assert security_types_from_profiles(prof)['ABC' + suffix] == 'secondary'


@pytest.mark.parametrize('suffix', ['A', 'B', 'C', 'K', 'AA', 'XY', 'LA'])
def test_class_letter_and_other_suffixes_do_not_hit(suffix):
    prof = {'ABC': _p('Abc Inc'), 'ABC' + suffix: _p('Abc Inc')}
    assert security_types_from_profiles(prof)['ABC' + suffix] == 'stock'


def test_adr_not_hit():
    prof = {'BBD': _p('Banco Bradesco'), 'BBDO': _p('Banco Bradesco', adr=True)}
    assert security_types_from_profiles(prof)['BBDO'] == 'adr'


def test_different_cik_or_name_not_hit():
    assert security_types_from_profiles(
        {'ABC': _p('Abc Inc'), 'ABCO': _p('Abc Inc', cik='222')})['ABCO'] == 'stock'
    assert security_types_from_profiles(
        {'ABC': _p('Abc Inc'), 'ABCO': _p('Abc Inc Ltd')})['ABCO'] == 'stock'
    # empty cik never groups
    assert security_types_from_profiles(
        {'ABC': _p('Abc Inc', cik=''), 'ABCO': _p('Abc Inc', cik='')})['ABCO'] == 'stock'


def test_symbol_must_extend_a_group_member():
    prof = {'ABC': _p('Abc Inc'), 'XYZO': _p('Abc Inc')}
    assert security_types_from_profiles(prof)['XYZO'] == 'stock'


def test_googl_allow_list():
    prof = {'GOO': _p('Alphabet Inc.'), 'GOOGL': _p('Alphabet Inc.'), 'GOOG': _p('Alphabet Inc.')}
    t = security_types_from_profiles(prof)
    assert t['GOOGL'] == 'stock' and t['GOOG'] == 'stock'


def test_deny_list_hits_only_when_present_and_typed_stock():
    assert 'MGRB' in SECONDARY_DENY_LIST and len(SECONDARY_DENY_LIST) == 32
    prof = {'MGRB': _p('Affiliated Managers Group', cik='9'),   # no parent-suffix relation
            'STRC': _p('Strategy Inc Preferred Stock', cik='8'),  # already 'pref'
            'KKRS': {'_empty': True}}                              # unknown -> not forced
    t = security_types_from_profiles(prof)
    assert t['MGRB'] == 'secondary'
    assert t['STRC'] == 'pref'
    assert 'KKRS' not in t and 'APOS' not in t                      # absent never invented


def test_missing_and_malformed_profiles_tolerated():
    assert security_types_from_profiles(None) == {}
    assert security_types_from_profiles({'A': None, 'B': 'x', 'C': {}}) == {}


def test_writer_secondary_line_takes_no_russell_slot():
    from src.pipeline import ticker_metadata_writer as w

    def alp(s):
        return {'symbol': s, 'asset_class': 'us_equity', 'exchange': 'NYSE', 'status': 'active',
                'tradable': True, 'shortable': True, 'fractionable': True,
                'easy_to_borrow': True, 'first_seen_at': '2000-01-01',
                'last_seen_at': '2026-10-01'}
    prof = {'NTRS': _p('Northern Trust Corporation', mktCap=3.0e10),
            'NTRSO': _p('Northern Trust Corporation', mktCap=3.0e10),
            'ZZZ': _p('Zzz Corp', cik='5', mktCap=1.0e6)}
    rows = w.build_metadata_rows(date(2026, 10, 2), [alp(s) for s in prof], prof, {}, {},
                                 source_tag='t')
    by = {r['symbol']: r for r in rows}
    assert by['NTRSO']['security_type'] == 'secondary'
    assert by['NTRSO']['in_r1000'] is False and by['NTRSO']['in_r3000'] is False
    assert by['NTRS']['in_r1000'] is True and by['ZZZ']['in_r3000'] is True


def test_writer_computes_cache_wide_types_once(monkeypatch):
    from src.pipeline import ticker_metadata_writer as w
    calls = []
    real = w.security_types_from_profiles
    monkeypatch.setattr(w, 'security_types_from_profiles',
                        lambda p: calls.append(1) or real(p))
    prof = {s: _p('N%d' % i, cik=str(i)) for i, s in enumerate(['A1', 'A2', 'A3'])}
    alp = [{'symbol': s, 'asset_class': 'us_equity', 'exchange': 'N', 'status': 'active'}
           for s in prof]
    w.build_metadata_rows(date(2026, 10, 2), alp, prof, {}, {}, source_tag='t')
    assert len(calls) == 1


def test_every_consumer_uses_the_cache_wide_function(tmp_path):
    prof = {'NTRS': _p('Northern Trust Corporation'),
            'NTRSO': _p('Northern Trust Corporation')}
    # membership overlay
    from scripts import build_tier_membership as btm
    assert btm.build_security_type_overlay({}, prof)['NTRSO'] == 'secondary'
    # rank-flag repair fallback
    from scripts import rederive_rank_flags as rrf
    pc = tmp_path / 'p.json'
    pc.write_text(json.dumps(prof))
    assert rrf.load_overlay({}, pc)['NTRSO'] == 'secondary'
    # fund-exposure report: secondary counts with the non-common share
    from scripts import report_fund_exposure as rep
    s = rep.fund_share(['NTRSO', 'NTRS'], prof)
    assert (s['total'], s['fund'], s['unknown']) == (2, 1, 0)
    assert rep.ticker_security_type('NTRSO', prof) == 'secondary'


_CACHE = Path('/root/openclaw/data/.cache/fmp_profile.json')


@pytest.mark.skipif(not _CACHE.exists(), reason='vendor profile cache not on this box')
def test_real_cache_secondary_lines():
    d = json.loads(_CACHE.read_text())
    t = security_types_from_profiles(d)
    for sym in ('NTRSO', 'TPGXL', 'HBANP', 'ZIONP', 'AGNCN', 'MGRB', 'KKRS'):
        assert t.get(sym) == 'secondary', sym
    for sym in ('GOOGL', 'GOOG', 'FOXA', 'NWSA', 'UAA', 'AAPL', 'NTRS', 'TPG', 'HBAN'):
        assert t.get(sym) == 'stock', sym
    assert t.get('BBDO') != 'secondary'
    # per-profile typing is a subset-preserving refinement: only stock -> secondary
    for sym, ty in t.items():
        base = security_type_from_profile(d[sym])
        assert ty == base or (base == 'stock' and ty == 'secondary'), sym
    assert sum(1 for v in t.values() if v == 'secondary') > 100
