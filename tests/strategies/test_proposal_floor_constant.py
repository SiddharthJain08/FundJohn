"""D2a — ONE auto-approve confidence floor constant (spec 2026-09-12 §4 D2).

Before: auto_approve defaulted to 0.85 (:311) while auto_apply_batch defaulted
to 0.8 (:403) and the CLI help said 0.8 (:473). Three reads, two answers.

No DB: every test either calls the pure accessor or greps the module source.

Run only this file:
  cd /root/openclaw/.claude/worktrees/qd-adoptions && \
    python3 -m pytest tests/strategies/test_proposal_floor_constant.py -q
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import proposal_manager as pm  # noqa: E402

SOURCE = (ROOT / 'src' / 'strategies' / 'proposal_manager.py').read_text()


def test_default_is_085():
    assert pm.DEFAULT_AUTOAPPROVE_MIN_CONFIDENCE == 0.85


def test_accessor_returns_the_default_when_env_unset(monkeypatch):
    monkeypatch.delenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', raising=False)
    assert pm.autoapprove_min_confidence() == 0.85


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.9')
    assert pm.autoapprove_min_confidence() == 0.9


def test_env_set_empty_raises_fail_closed(monkeypatch):
    """Precedence must stay byte-identical to the pre-unification reads:
    float(os.environ.get(KEY, default)) raised on KEY='' because
    os.environ.get returns the actual (empty) value, not the default, once
    the key exists. A silent fallback here would quietly loosen the floor
    below what the operator set in .env, so this must still raise."""
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '')
    try:
        pm.autoapprove_min_confidence()
        assert False, 'expected ValueError'
    except ValueError:
        pass


def test_env_set_garbage_raises_fail_closed(monkeypatch):
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', 'not-a-float')
    try:
        pm.autoapprove_min_confidence()
        assert False, 'expected ValueError'
    except ValueError:
        pass


def test_no_literal_08_or_085_default_survives_in_the_source():
    """Exactly one place may spell the number: the constant itself."""
    leftovers = re.findall(
        r"OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE['\"]\s*,\s*['\"]0\.\d+['\"]", SOURCE)
    assert leftovers == [], f'inline env defaults still present: {leftovers}'
    assert SOURCE.count('DEFAULT_AUTOAPPROVE_MIN_CONFIDENCE = 0.85') == 1


_INLINE_DEFAULT_PATTERNS = (
    # Python: os.environ.get('OPENCLAW_...MIN_CONFIDENCE', '0.8') /
    # os.getenv('OPENCLAW_...MIN_CONFIDENCE', '0.8'). Anchored on the actual
    # get-with-default call (not monkeypatch.setenv/delenv, which legitimately
    # name the var in tests without encoding a duplicate default).
    re.compile(
        r"os\.(?:environ\.get|getenv)\(\s*['\"]"
        r"OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE['\"]\s*,\s*['\"]?[0-9.]+"),
    # JS equivalents: process.env.OPENCLAW_...MIN_CONFIDENCE || 0.8 /
    # ?? '0.8' / process.env['...'] || 0.8
    re.compile(
        r"process\.env(?:\.OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE|"
        r"\[['\"]OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE['\"]\])"
        r"\s*(\|\||\?\?)\s*['\"]?[0-9.]+"),
)


def test_no_other_file_hardcodes_a_default_for_the_floor_env_var():
    """Ledger row T3<->T5: 'a source-grep test forbids inline literals of
    the floor anywhere else.' proposal_manager.py's constant + accessor are
    the ONE place allowed to pair OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE
    with a numeric fallback. Scoped to the inline-default pattern (not every
    mention of the var name) so it doesn't false-positive on
    test_proposal_auto_approval.py's monkeypatch.setenv(...) calls, which
    legitimately name the var without encoding a duplicate default."""
    exempt = {
        (ROOT / 'src' / 'strategies' / 'proposal_manager.py').resolve(),
        Path(__file__).resolve(),
    }
    offenders = []
    for path in ROOT.rglob('*'):
        if path.suffix not in ('.py', '.js'):
            continue
        parts = set(path.parts)
        if '__pycache__' in parts or 'node_modules' in parts or '.git' in parts:
            continue
        if path.resolve() in exempt:
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if any(p.search(text) for p in _INLINE_DEFAULT_PATTERNS):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], f'inline floor default found outside proposal_manager.py: {offenders}'


def test_auto_apply_batch_threshold_uses_the_accessor(monkeypatch):
    """threshold=None must resolve through autoapprove_min_confidence()."""
    seen = {}
    monkeypatch.setattr(pm, 'autoapprove_min_confidence', lambda: 0.77)
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', '1')
    monkeypatch.setattr(pm, 'list_proposals', lambda **kw: [])
    result = pm.auto_apply_batch(log=lambda m: seen.setdefault('log', []).append(m))
    assert result['threshold'] == 0.77


def test_cli_help_text_does_not_hardcode_08():
    assert 'or 0.8)' not in SOURCE
