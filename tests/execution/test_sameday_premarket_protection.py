"""Pre-market protection under same-day execution (2026-07-30).

Under the same-day pivot signals are COMPUTED at 15:00[T] and FILLED by
15:55[T], so at 09:15[T+1] there is never a COMPUTED row with
target_date=T+1. Both pre-market jobs were therefore inert: the gate had no
subjects to score, and the reconcile's APPROVED target was empty every single
morning — two of its three flatten conditions permanently met, with only a
fail-closed health sentinel standing between the book and a full liquidation
(verified live 2026-07-29: `REFUSING FLATTEN of 6 positions … holds=6`).

Same-day protect mode re-points both at the thing we actually carry overnight:
the broker book. These pin the seams that make that safe.

Pure unit tests — fake cursors, injected broker loaders, dry_run for the
reconcile. No DB, no broker, no orders.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import open_reconcile as orec  # noqa: E402
from execution import premarket_gate as pg  # noqa: E402

TODAY = date(2026, 7, 30)


@pytest.fixture
def sameday(monkeypatch):
    monkeypatch.setenv('OPENCLAW_SAMEDAY_EXEC', '1')
    monkeypatch.delenv('OPENCLAW_SAMEDAY_PREMARKET_PROTECT', raising=False)
    monkeypatch.setenv('OPENCLAW_EOD_PREMARKET_GATE', '1')
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')


class _Cur:
    """Records every statement; answers the handful of SELECTs these paths make."""

    def __init__(self, vetoes=(), gate_ran=True):
        self.statements: list[tuple[str, tuple]] = []
        self._vetoes = list(vetoes)
        self._gate_ran = gate_ran
        self._result: list = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        s = ' '.join(sql.split())
        if "gate_type = 'premarket_hold'" in s and 'SELECT DISTINCT ticker' in s:
            self._result = [(t,) for t in self._vetoes]
        elif "gate_type = '__gate_ran__'" in s:
            self._result = [(1,)] if self._gate_ran else []
        else:
            self._result = []

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None

    def wrote(self, needle):
        return [p for sql, p in self.statements if needle in ' '.join(sql.split())]


class _Conn:
    def __init__(self, cur):
        self._cur = cur
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1


class TestModeSelection:
    def test_on_in_sameday_by_default(self, sameday):
        assert pg.sameday_protect_mode() is True
        assert orec._sameday_protect_mode() is True

    def test_off_outside_sameday(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_SAMEDAY_EXEC', '0')
        assert pg.sameday_protect_mode() is False
        assert orec._sameday_protect_mode() is False

    def test_escape_hatch_reverts_without_a_code_change(self, sameday, monkeypatch):
        monkeypatch.setenv('OPENCLAW_SAMEDAY_PREMARKET_PROTECT', '0')
        assert pg.sameday_protect_mode() is False
        assert orec._sameday_protect_mode() is False

    def test_gate_and_reconcile_never_disagree(self, monkeypatch):
        """A split decision would be the worst case: the gate vetoes against the
        book while the reconcile still diffs an empty APPROVED register."""
        for exec_flag in ('0', '1'):
            for protect in (None, '0', '1'):
                monkeypatch.setenv('OPENCLAW_SAMEDAY_EXEC', exec_flag)
                if protect is None:
                    monkeypatch.delenv('OPENCLAW_SAMEDAY_PREMARKET_PROTECT', raising=False)
                else:
                    monkeypatch.setenv('OPENCLAW_SAMEDAY_PREMARKET_PROTECT', protect)
                assert pg.sameday_protect_mode() == orec._sameday_protect_mode()


class TestHeldSubjects:
    def test_book_becomes_signed_subjects(self):
        subs = pg._load_held_subjects(lambda: {'AAPL': 12000.0, 'TSLA': -4000.0})
        by = {s['ticker']: s for s in subs}
        assert by['AAPL']['direction'] == 'LONG'
        assert by['TSLA']['direction'] == 'SHORT'
        assert by['TSLA']['position_size_pct'] == 4000.0

    def test_subjects_carry_no_signal_id(self):
        """id=None is what stops the gate rewriting a FILLED execution_signals
        row to REJECTED — that row is the ledger entry for a live position."""
        subs = pg._load_held_subjects(lambda: {'AAPL': 1.0})
        assert subs[0]['id'] is None

    def test_option_and_crypto_legs_excluded(self):
        subs = pg._load_held_subjects(lambda: {
            'AAPL': 1000.0,
            'AAPL260821C00220000': 500.0,   # OCC leg — option lane owns its closes
            'BTC/USD': 900.0,               # not a news-gateable equity
            'FLAT': 0.0,
        })
        assert [s['ticker'] for s in subs] == ['AAPL']

    def test_broker_failure_is_loud_and_empty(self, caplog):
        """Returning [] silently would look identical to a flat book."""
        def boom():
            raise RuntimeError('broker down')
        with caplog.at_level('ERROR'):
            assert pg._load_held_subjects(boom) == []
        assert any('UNPROTECTED' in r.message for r in caplog.records)


class TestGateScoresTheBook:
    def _run(self, monkeypatch, *, panic, book, confirmer_verdict='bearish_news_driven',
             alert=None):
        # NEVER let a test reach the real webhook. Harmless without
        # POSTGRES_URI (the lookup cannot connect), but pytest is routinely run
        # with it exported on this box — which would post a fabricated veto to
        # the operator's #pre-market-alerts.
        monkeypatch.setattr(pg, '_post_premarket_alert', alert or (lambda v, d: None))
        monkeypatch.setattr(pg, 'score_news_for_tickers',
                            lambda tickers, since: [
                                {'ticker': t, 'news_count_24h': 5, 'news_finbert_neg': 4,
                                 'news_mean_score': -0.8, 'news_top_headlines': ['h'],
                                 'evidence_uuids': ['u']} for t in tickers])
        monkeypatch.setattr(pg, 'panic_score', lambda inp: panic)
        monkeypatch.setattr(pg, 'confirm_panic', lambda inp: type(
            'R', (), {'verdict': confirmer_verdict, 'severity': 4, 'rationale': ''})())
        monkeypatch.setattr(pg, '_load_carried_signals', lambda cur, d: [])
        cur = _Cur()
        conn = _Conn(cur)
        out = pg.run_gate(conn=conn, broker_loader=lambda: book)
        return out, cur

    def test_held_position_is_scored_and_vetoed(self, sameday, monkeypatch):
        out, cur = self._run(monkeypatch, panic=90.0, book={'AAPL': 5000.0})
        assert out['gate_ran'] is True
        assert out['n_processed'] == 1 and out['n_rejected'] == 1
        verdicts = cur.wrote('INSERT INTO signal_gate_verdicts')
        hold_rows = [p for p in verdicts if p[1] == pg.GATE_TYPE_HOLD]
        assert len(hold_rows) == 1
        assert hold_rows[0][0] is None          # signal_id
        assert hold_rows[0][2] == 'AAPL'        # ticker
        assert hold_rows[0][4] == 'REJECTED'    # verdict

    def test_calm_news_leaves_the_position_alone(self, sameday, monkeypatch):
        out, cur = self._run(monkeypatch, panic=1.0, book={'AAPL': 5000.0})
        assert out['n_rejected'] == 0 and out['n_approved'] == 1

    def test_filled_signal_rows_are_never_transitioned(self, sameday, monkeypatch):
        """The veto must not touch execution_signals: the row that opened the
        position is FILLED, and the P&L ledger hangs off it."""
        _, cur = self._run(monkeypatch, panic=90.0, book={'AAPL': 5000.0})
        assert cur.wrote('UPDATE execution_signals') == []

    def test_sentinel_still_written(self, sameday, monkeypatch):
        _, cur = self._run(monkeypatch, panic=1.0, book={'AAPL': 1.0})
        verdicts = cur.wrote('INSERT INTO signal_gate_verdicts')
        assert any(p[1] == pg.GATE_TYPE_SENTINEL for p in verdicts)

    def test_book_ignored_outside_sameday_mode(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_EOD_PREMARKET_GATE', '1')
        monkeypatch.setenv('OPENCLAW_SAMEDAY_EXEC', '0')
        out, cur = self._run(monkeypatch, panic=90.0, book={'AAPL': 5000.0})
        assert out['n_processed'] == 0
        assert [p for p in cur.wrote('INSERT INTO signal_gate_verdicts')
                if p[1] == pg.GATE_TYPE_HOLD] == []


class TestHoldTargetSet:
    def test_book_is_the_target_minus_vetoes(self):
        t = orec._hold_target_set({'AAPL': 100.0, 'TSLA': -50.0, 'NVDA': 10.0}, {'TSLA'})
        assert t == {'AAPL': 1.0, 'NVDA': 1.0}

    def test_signs_mirror_the_book_so_nothing_reads_as_a_flip(self):
        t = orec._hold_target_set({'TSLA': -50.0}, set())
        assert t['TSLA'] == -1.0


class TestReconcileTargets:
    def _run(self, book, vetoes, *, gate_ran=True, dry_run=True):
        cur = _Cur(vetoes=vetoes, gate_ran=gate_ran)
        conn = _Conn(cur)
        counts = orec.run_reconcile(dry_run=dry_run, conn=conn,
                                    broker_loader=lambda: dict(book),
                                    run_date=TODAY)
        return counts, cur

    def test_no_vetoes_closes_nothing(self, sameday):
        """The whole point: an empty signal register must no longer read as
        'everything was dropped'."""
        counts, _ = self._run({'AAPL': 100.0, 'TSLA': -50.0}, [])
        assert counts['drops'] == 0 and counts['flattens'] == 0
        assert counts['holds'] == 2

    def test_vetoed_name_is_closed_and_only_it(self, sameday):
        counts, _ = self._run({'AAPL': 100.0, 'TSLA': -50.0}, ['TSLA'])
        assert counts['drops'] == 1 and counts['flattens'] == 0

    def test_all_vetoed_with_gate_ran_flattens(self, sameday):
        counts, _ = self._run({'AAPL': 100.0, 'TSLA': -50.0}, ['AAPL', 'TSLA'])
        assert counts['flattens'] == 2 and counts['holds'] == 0

    def test_all_vetoed_but_gate_did_not_run_refuses(self, sameday):
        """gate_ran is the only guard left in this mode, so it has to hold."""
        counts, _ = self._run({'AAPL': 100.0, 'TSLA': -50.0}, ['AAPL', 'TSLA'],
                              gate_ran=False)
        assert counts['flattens'] == 0 and counts['holds'] == 2

    def test_health_sentinel_is_not_consulted(self, sameday):
        """In same-day mode eod_compute_health for day T is written at 15:00,
        AFTER this 09:25 job — requiring it would refuse every flatten forever
        and silently disarm the protection when it is market-wide."""
        _, cur = self._run({'AAPL': 100.0}, ['AAPL'])
        assert cur.wrote('FROM eod_compute_health') == []

    def test_approved_register_untouched_outside_sameday(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
        monkeypatch.setenv('OPENCLAW_SAMEDAY_EXEC', '0')
        calls = []
        monkeypatch.setattr(orec, '_load_approved_set',
                            lambda cur, d: calls.append(d) or {'AAPL': 1.0})
        counts, _ = self._run({'AAPL': 100.0}, ['AAPL'])
        assert calls == [TODAY]          # legacy path taken
        assert counts['drops'] == 0      # the veto is ignored off-mode


class TestSizerReentryBlock:
    """Without this the 09:25 close is undone by the same day's 15:00 chain —
    the SNDK pattern (risk close re-bought hours later)."""

    @pytest.fixture(autouse=True)
    def _hygiene_on(self, monkeypatch):
        # tests/execution/conftest.py disables entry hygiene for the sizer e2e
        # suite; this class is its unit test, so re-enable it explicitly.
        monkeypatch.setenv('OPENCLAW_ENTRY_HYGIENE', '1')

    def test_vetoed_ticker_cannot_be_reopened(self):
        from execution import regime_blended_sizer as rbs
        out = rbs._apply_entry_hygiene_gate(
            {'AAPL': 10_000.0}, {}, stopouts={}, liq=({}, {}),
            params={'stopout_cooldown_days': 7, 'entry_min_adv_usd': 0,
                    'entry_min_price_usd': 0, 'entry_participation_frac': 1.0},
            premarket_vetoes={'AAPL'})
        assert 'AAPL' not in out

    def test_veto_is_direction_agnostic(self):
        """Unlike the stop-out cooldown, a news veto says 'this name is
        dangerous today', not 'this side of it lost'."""
        from execution import regime_blended_sizer as rbs
        out = rbs._apply_entry_hygiene_gate(
            {'AAPL': -10_000.0}, {}, stopouts={}, liq=({}, {}),
            params={'stopout_cooldown_days': 7, 'entry_min_adv_usd': 0,
                    'entry_min_price_usd': 0, 'entry_participation_frac': 1.0},
            premarket_vetoes={'AAPL'})
        assert 'AAPL' not in out

    def test_only_sheds_never_opens(self):
        """Only-shed semantics: a still-held vetoed name is capped at the held
        size, not flipped or grown."""
        from execution import regime_blended_sizer as rbs
        out = rbs._apply_entry_hygiene_gate(
            {'AAPL': 20_000.0}, {'AAPL': 5_000.0}, stopouts={}, liq=({}, {}),
            params={'stopout_cooldown_days': 7, 'entry_min_adv_usd': 0,
                    'entry_min_price_usd': 0, 'entry_participation_frac': 1.0},
            premarket_vetoes={'AAPL'})
        assert out['AAPL'] == 5_000.0

    def test_unvetoed_targets_pass_through(self):
        from execution import regime_blended_sizer as rbs
        out = rbs._apply_entry_hygiene_gate(
            {'MSFT': 10_000.0}, {}, stopouts={}, liq=({}, {}),
            params={'stopout_cooldown_days': 7, 'entry_min_adv_usd': 0,
                    'entry_min_price_usd': 0, 'entry_participation_frac': 1.0},
            premarket_vetoes={'AAPL'})
        assert out == {'MSFT': 10_000.0}


class TestVetoAlert:
    def test_veto_is_announced(self, sameday, monkeypatch):
        posted = {}
        t = TestGateScoresTheBook()
        t._run(monkeypatch, panic=90.0, book={'AAPL': 5000.0},
               alert=lambda v, d: posted.update({'vetoed': v, 'date': d}))
        assert [x[0] for x in posted['vetoed']] == ['AAPL']

    def test_no_veto_no_alert(self, sameday, monkeypatch):
        posted = []
        t = TestGateScoresTheBook()
        t._run(monkeypatch, panic=1.0, book={'AAPL': 5000.0},
               alert=lambda v, d: posted.append(v))
        assert posted == []

    def test_alert_failure_cannot_un_veto_a_position(self, sameday, monkeypatch):
        """The verdict rows are durable before the alert runs. If an alerting
        error reached the outer handler it would roll the whole gate back and
        report gate_ran=False — a Discord outage must never be able to
        resurrect a position the gate decided to close."""
        def boom(v, d):
            raise RuntimeError('discord down')
        t = TestGateScoresTheBook()
        out, cur = t._run(monkeypatch, panic=90.0, book={'AAPL': 5000.0}, alert=boom)
        assert out['gate_ran'] is True
        assert out['n_rejected'] == 1
        assert any('veto_alert_error' in e for e in out['errors'])
        assert [p for p in cur.wrote('INSERT INTO signal_gate_verdicts')
                if p[1] == pg.GATE_TYPE_HOLD][0][4] == 'REJECTED'
