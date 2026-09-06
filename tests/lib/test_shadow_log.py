# tests/lib/test_shadow_log.py
from __future__ import annotations
import datetime as dt
import re

import pytest

from lib import shadow_log


def test_record_appends_timestamped_line(tmp_path, monkeypatch):
    monkeypatch.setenv(shadow_log.DIR_ENV, str(tmp_path))
    p = shadow_log.record('rf_shadow', '[rf_shadow] site=bench_realized const=0.30 macro=0.28 n=20')
    assert p == tmp_path / 'rf_shadow.log'
    assert p.exists()
    lines = p.read_text().splitlines()
    assert len(lines) == 1
    stamp, rest = lines[0].split(' ', 1)
    assert rest == '[rf_shadow] site=bench_realized const=0.30 macro=0.28 n=20'
    # 'YYYY-MM-DDTHH:MM:SSZ', parseable as UTC.
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', stamp)
    dt.datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ')


def test_record_two_calls_append_in_order(tmp_path, monkeypatch):
    monkeypatch.setenv(shadow_log.DIR_ENV, str(tmp_path))
    shadow_log.record('options_surface_shadow', 'first line')
    p = shadow_log.record('options_surface_shadow', 'second line')
    assert p == tmp_path / 'options_surface_shadow.log'
    lines = p.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith('first line')
    assert lines[1].endswith('second line')


def test_record_unwritable_dir_returns_none_without_raising(tmp_path, monkeypatch):
    # A FILE where the dir should be -> mkdir(parents=True, exist_ok=True) raises.
    blocker = tmp_path / 'blocked'
    blocker.write_text('not a directory')
    monkeypatch.setenv(shadow_log.DIR_ENV, str(blocker))
    assert shadow_log.record('rf_shadow', 'whatever') is None


def test_shadow_dir_defaults_to_root_logs(monkeypatch):
    monkeypatch.delenv(shadow_log.DIR_ENV, raising=False)
    assert shadow_log.shadow_dir() == shadow_log.ROOT / 'logs'
