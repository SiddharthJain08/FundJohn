"""tests/execution/test_run_lock_wiring.py — orchestrator uses the shared,
owned, renewed run lock and exits rc=75 when another run owns today.

Fake Redis only. No REAL pipeline script is ever spawned: run_step tests use
harmless one-shot binaries (`true`/`false`) via a stubbed `_resolve_script`
(TestRunStepRenews / TestRunStepRenewRetry actually invoke subprocess.Popen
on those binaries — that's a real subprocess, just not a real pipeline
script). Every test that drives `main()` goes through `_hermetic_main()`,
which forces POSTGRES_URI to a stub value, replaces every Discord/DB/
dashboard call site (`pipeline_feed`, `data_alerts`, `set_agent_status`,
`broadcast_dashboard_refresh`, `notify`, `get_redis`), and — belt and
suspenders — patches `http.client.HTTPConnection` to raise if anything
still tries a real HTTP connection. Nothing here touches a real Redis,
Postgres, Discord webhook, or the live dashboard on :3000.
"""
from __future__ import annotations

import contextlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import pipeline_orchestrator as po  # noqa: E402
from lib import run_lock  # noqa: E402
from lib import capped_spawn as _capped_spawn  # noqa: E402


def setUpModule():
    # QD E2 hermeticity: run_step now calls capped_spawn.wrap_capped(), whose
    # module-global `_STATE['available']` defaults to None (unresolved) and,
    # on an unresolved probe, actually shells out to `systemd-run` the first
    # time any test here calls run_step — on this box (uid 0, systemd-run
    # present) that is a REAL transient scope, exactly what this file's
    # "no real subprocess but the stub scripts" contract forbids. Pin it OFF
    # for the whole module (matches pre-QD-E2 behaviour: unwrapped argv) so
    # every run_step-calling test here — old and new — stays hermetic
    # regardless of pytest invocation order or what other test module ran
    # before this one. `TestRunStepIsCapped` overrides per-test as needed.
    _capped_spawn._reset(available=False)


def tearDownModule():
    _capped_spawn._reset()


class FakeRedis:
    def __init__(self, store=None):
        self.store = dict(store or {})
        self.ttls = {}

    def set(self, k, v, nx=False, ex=None):
        if nx and k in self.store:
            return None
        self.store[k] = v
        if ex is not None:
            self.ttls[k] = ex
        return True

    def get(self, k):
        return self.store.get(k)

    def delete(self, k):
        self.store.pop(k, None)
        self.ttls.pop(k, None)

    def expire(self, k, ttl):
        if k not in self.store:
            return False
        self.ttls[k] = ttl
        return True

    def setex(self, k, ttl, v):
        self.store[k] = v
        self.ttls[k] = ttl

    def publish(self, *a, **kw):
        return 0


DATE = '2026-09-14'
KEY = f'pipeline:run_lock:{DATE}'


def _raise_if_touched(*a, **kw):
    raise AssertionError(
        'a test tried to open a real HTTP connection (http.client.'
        'HTTPConnection) — stub the call site instead')


@contextlib.contextmanager
def _hermetic_main(r):
    """Patch every real-I/O surface `main()` can reach, for every test that
    drives `po.main()` — a test whose cycle runs to completion must never
    touch a real Redis, Postgres, Discord webhook, or the live dashboard's
    HTTP endpoint on this production host.

    POSTGRES_URI is FORCED via `mock.patch.dict` (never `os.environ.
    setdefault`): this host exports a real value via `.env`, so setdefault
    is a no-op there and every psycopg2 call inside `set_agent_status` /
    `_load_channel_webhooks` would attempt a real connection.

    `http.client.HTTPConnection` is patched to raise if ANY code reaches
    it — belt-and-suspenders alongside stubbing `broadcast_dashboard_
    refresh` directly, so a future refactor that bypasses the module-level
    stub still fails loudly in CI instead of POSTing to the live dashboard.

    Yields (posts, feed_msgs, alert_msgs, dashboard_calls) for the caller to
    assert against. `run_step` / `_resolve_script` are snapshotted and
    ALWAYS restored on exit, but left as the real implementations for the
    caller to override (fully stubbed, wrapped, or left alone) inside the
    `with` block — different tests need different treatment.
    """
    posts: list[tuple[str, str]] = []
    feed_msgs: list[str] = []
    alert_msgs: list[str] = []
    dashboard_calls: list[str] = []

    orig = (po.get_redis, po.notify, po.pipeline_feed, po.data_alerts,
            po.set_agent_status, po.broadcast_dashboard_refresh,
            po.is_completed_today, po.read_checkpoint,
            po.run_step, po._resolve_script)
    po.get_redis = lambda: r
    po.notify = lambda msg, channel='pipeline-feed': posts.append((channel, msg))
    po.pipeline_feed = lambda msg: feed_msgs.append(msg)
    po.data_alerts = lambda msg: alert_msgs.append(msg)
    po.set_agent_status = lambda *a, **kw: None
    po.broadcast_dashboard_refresh = lambda run_date: dashboard_calls.append(run_date)
    po.is_completed_today = lambda _r, _d: False
    po.read_checkpoint = lambda _r: None
    try:
        with mock.patch.dict(os.environ, {'POSTGRES_URI': 'postgresql://stub/stub'}), \
             mock.patch('http.client.HTTPConnection', side_effect=_raise_if_touched):
            yield posts, feed_msgs, alert_msgs, dashboard_calls
    finally:
        (po.get_redis, po.notify, po.pipeline_feed, po.data_alerts,
         po.set_agent_status, po.broadcast_dashboard_refresh,
         po.is_completed_today, po.read_checkpoint,
         po.run_step, po._resolve_script) = orig


class TestLockHelpers(unittest.TestCase):
    def setUp(self):
        po.LOCK_VALUE = None

    def tearDown(self):
        po.LOCK_VALUE = None

    def test_acquire_writes_the_shared_key_with_an_owned_value(self):
        r = FakeRedis()
        self.assertTrue(po.acquire_lock(r, DATE))
        self.assertIn(KEY, r.store)
        self.assertIsNotNone(run_lock.parse_value(r.store[KEY]))
        self.assertEqual(run_lock.parse_value(r.store[KEY])[1], os.getpid())
        self.assertEqual(po.LOCK_VALUE, r.store[KEY])

    def test_acquire_refuses_when_a_live_holder_owns_the_key(self):
        r = FakeRedis({KEY: f'{po._HOST}:{os.getpid()}:T0'})
        self.assertFalse(po.acquire_lock(r, DATE))

    def test_acquire_stashes_the_holder_for_the_caller_on_refusal(self):
        r = FakeRedis({KEY: f'{po._HOST}:{os.getpid()}:T0'})
        self.assertFalse(po.acquire_lock(r, DATE))
        self.assertEqual(po._LAST_HOLDER, f'{po._HOST}:{os.getpid()}:T0')

    def test_renew_extends_our_ttl_to_step_timeout_plus_120(self):
        r = FakeRedis()
        po.acquire_lock(r, DATE)
        self.assertTrue(po.renew_lock(r, DATE, run_lock.ttl_for(9000)))
        self.assertEqual(r.ttls[KEY], 9120)

    def test_release_is_value_checked(self):
        r = FakeRedis()
        po.acquire_lock(r, DATE)
        r.store[KEY] = 'otherbox:222:T9'          # someone took over
        self.assertFalse(po.release_lock(r, DATE))
        self.assertEqual(r.store[KEY], 'otherbox:222:T9')

    def test_release_deletes_our_own_lock(self):
        r = FakeRedis()
        po.acquire_lock(r, DATE)
        self.assertTrue(po.release_lock(r, DATE))
        self.assertNotIn(KEY, r.store)


class TestRunStepRenews(unittest.TestCase):
    def test_run_step_calls_renew_with_the_resolved_step_timeout(self):
        seen = []
        # `false` exits 1 immediately; we only care that renew fired first.
        orig = po._resolve_script
        po._resolve_script = lambda script, run_date: (['false'], 777)
        try:
            ok, rc = po.run_step('engine', DATE, dict(os.environ),
                                 renew=lambda t: seen.append(t))
        finally:
            po._resolve_script = orig
        self.assertFalse(ok)
        self.assertEqual(seen, [777])

    def test_run_step_without_renew_is_unchanged(self):
        orig = po._resolve_script
        po._resolve_script = lambda script, run_date: (['true'], 5)
        try:
            ok, rc = po.run_step('engine', DATE, dict(os.environ))
        finally:
            po._resolve_script = orig
        self.assertTrue(ok)
        self.assertEqual(rc, 0)


class TestRunStepRenewRetry(unittest.TestCase):
    """Controller ruling (fix round 1): a renew() that RAISES (rather than
    cleanly reporting False) is tolerated once — a 2s-later retry — before
    being treated as fatal. `time.sleep` is stubbed so these stay fast.
    """

    def setUp(self):
        self._orig_resolve = po._resolve_script
        self._orig_sleep = po.time.sleep
        po._resolve_script = lambda script, run_date: (['true'], 5)
        po.time.sleep = lambda s: None

    def tearDown(self):
        po._resolve_script = self._orig_resolve
        po.time.sleep = self._orig_sleep

    def test_renew_raising_once_then_succeeding_runs_the_step(self):
        calls = []

        def _renew(t):
            calls.append(t)
            if len(calls) == 1:
                raise ConnectionError('redis blip')
            # second call: succeeds (real callback returns None too)

        ok, rc = po.run_step('engine', DATE, dict(os.environ), renew=_renew)
        self.assertTrue(ok)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 2)   # first attempt + one retry

    def test_renew_raising_twice_is_fatal_and_the_step_never_spawns(self):
        calls = []

        def _renew(t):
            calls.append(t)
            raise ConnectionError('redis blip')

        orig_popen = po.subprocess.Popen
        po.subprocess.Popen = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError('no subprocess may spawn — renew already failed twice'))
        try:
            with self.assertRaises(po.LockLost):
                po.run_step('engine', DATE, dict(os.environ), renew=_renew)
        finally:
            po.subprocess.Popen = orig_popen
        self.assertEqual(len(calls), 2)   # exactly one retry, no more


class TestMainBusyLock(unittest.TestCase):
    def test_main_returns_75_and_posts_when_another_run_owns_today(self):
        r = FakeRedis({KEY: f'{po._HOST}:{os.getpid()}:T0'})
        with _hermetic_main(r) as (posts, feed_msgs, alert_msgs, dashboard_calls):
            po.run_step = lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError('no step may run'))
            rc = po.main(['--date', DATE, '--steps', 'report'])
        self.assertEqual(rc, 75)
        self.assertEqual(rc, run_lock.LOCK_BUSY_RC)
        self.assertTrue(any('[lock] held by' in m for _c, m in posts), posts)
        self.assertEqual(r.store[KEY], f'{po._HOST}:{os.getpid()}:T0')
        # Refused before the cycle starts — the final action never runs.
        self.assertEqual(dashboard_calls, [])


class TestForceResume(unittest.TestCase):
    """Controller ruling (fix round 1): --force-resume must NOT acquire,
    write, overwrite, or release the lock at all. The pre-fix-round-1
    version of this branch wrote an OWNED value over whatever was already
    there; since scripts/redeploy_pipeline.py hardcodes --force-resume onto
    every intraday redeploy, that write could clobber the DAILY CYCLE's own
    lock value mid-run and kill it at its very next renew — a failure mode
    this task introduced and that did not exist before it. Zero lock
    interaction preserves the old "runs beside the holder" semantics
    without ever being able to harm it: `LOCK_VALUE` is simply never
    assigned in this branch, so every `release_lock()` call (including the
    top-level `finally`) is a no-op.
    """

    def setUp(self):
        po.LOCK_VALUE = None

    def tearDown(self):
        po.LOCK_VALUE = None

    def _run(self, r):
        with _hermetic_main(r) as (posts, feed_msgs, alert_msgs, dashboard_calls):
            po.run_step = lambda *a, **kw: (True, 0)
            rc = po.main(['--date', DATE, '--force-resume', '--steps', 'report'])
        return rc, posts, dashboard_calls

    def test_force_resume_with_live_holder_leaves_it_byte_identical(self):
        r = FakeRedis({KEY: 'otherbox:99999:T0'})   # some other live holder
        rc, posts, dashboard_calls = self._run(r)
        self.assertEqual(rc, 0)
        # Byte-identical — not overwritten, not deleted.
        self.assertEqual(r.store[KEY], 'otherbox:99999:T0')
        self.assertIsNone(po.LOCK_VALUE)
        self.assertTrue(any('running beside holder' in m for _c, m in posts), posts)
        self.assertTrue(any('otherbox:99999:T0' in m for _c, m in posts), posts)
        self.assertEqual(dashboard_calls, [DATE])   # cycle still ran to completion

    def test_force_resume_with_no_holder_writes_nothing(self):
        r = FakeRedis()
        rc, posts, dashboard_calls = self._run(r)
        self.assertEqual(rc, 0)
        self.assertNotIn(KEY, r.store)               # nothing written
        self.assertIsNone(po.LOCK_VALUE)
        self.assertTrue(any('running beside holder' in m for _c, m in posts), posts)
        self.assertEqual(dashboard_calls, [DATE])


class TestRenewLostIsFatal(unittest.TestCase):
    """Controller ruling (Task 1 review, carried into this task's brief):
    a renew() that reports we no longer own the lock is FATAL — the
    orchestrator must stop before running the next step, log '[lock] lost',
    post to Discord, and exit rc=75. This is distinct from TestMainBusyLock
    (busy at acquire time, before any step runs): here the lock is acquired
    cleanly, the first step succeeds, and the SECOND renew (before the
    second step) reports loss — the harder, mid-cycle case the ruling names.

    `_resolve_script` is stubbed for speed/determinism; `run_step` itself is
    NOT stubbed, so the real renew wiring runs. Because main() also calls
    `_resolve_script` once per step up front to size the initial lock TTL,
    "which steps ran" is tracked by wrapping `run_step` itself (called
    exactly once per step attempted), not by counting `_resolve_script`
    calls.
    """

    def test_main_stops_before_the_next_step_when_renew_reports_lock_loss(self):
        r = FakeRedis()
        ran_scripts = []
        renew_calls = []
        orig_renew = run_lock.renew

        def _fake_renew(*a, **kw):
            renew_calls.append(1)
            return len(renew_calls) == 1

        run_lock.renew = _fake_renew
        try:
            with _hermetic_main(r) as (posts, feed_msgs, alert_msgs, dashboard_calls):
                po._resolve_script = lambda script, run_date: (['true'], 5)
                orig_run_step = po.run_step

                def _tracking_run_step(script, run_date, env, renew=None):
                    ran_scripts.append(script)
                    return orig_run_step(script, run_date, env, renew=renew)

                po.run_step = _tracking_run_step
                rc = po.main(['--date', DATE, '--steps', 'signals,handoff,trade'])
        finally:
            run_lock.renew = orig_renew

        self.assertEqual(rc, run_lock.LOCK_BUSY_RC)
        self.assertEqual(rc, 75)
        self.assertTrue(any('[lock] lost' in m for _c, m in posts), posts)
        # 'signals' (engine) ran and succeeded; 'handoff' (trade_handoff_
        # builder) was attempted and its renew lost the lock before spawn;
        # 'trade' (regime_blended_sizer_live) must never have been attempted
        # at all — the cycle stopped before its step, the actual claim.
        self.assertEqual(ran_scripts, ['engine', 'trade_handoff_builder'])
        # Aborted mid-cycle — the final action never runs.
        self.assertEqual(dashboard_calls, [])


class TestMainStopsWhenRenewRaisesTwice(unittest.TestCase):
    """Controller ruling (fix round 1): a renew() that RAISES (rather than
    cleanly returning False) is tolerated once, 2s later — but if the retry
    ALSO raises, that is just as fatal as an explicit False. Same shape as
    TestRenewLostIsFatal, with a raising renew instead of a False-returning
    one.
    """

    def test_main_stops_before_the_next_step_when_renew_raises_twice(self):
        r = FakeRedis()
        ran_scripts = []
        renew_calls = []
        orig_renew = run_lock.renew
        orig_sleep = po.time.sleep
        po.time.sleep = lambda s: None   # skip the real 2s retry delay

        def _fake_renew(*a, **kw):
            renew_calls.append(1)
            if len(renew_calls) == 1:
                return True   # first step's renew succeeds cleanly
            raise ConnectionError('redis blip')   # every renew after that raises

        run_lock.renew = _fake_renew
        try:
            with _hermetic_main(r) as (posts, feed_msgs, alert_msgs, dashboard_calls):
                po._resolve_script = lambda script, run_date: (['true'], 5)
                orig_run_step = po.run_step

                def _tracking_run_step(script, run_date, env, renew=None):
                    ran_scripts.append(script)
                    return orig_run_step(script, run_date, env, renew=renew)

                po.run_step = _tracking_run_step
                rc = po.main(['--date', DATE, '--steps', 'signals,handoff,trade'])
        finally:
            run_lock.renew = orig_renew
            po.time.sleep = orig_sleep

        self.assertEqual(rc, run_lock.LOCK_BUSY_RC)
        self.assertEqual(rc, 75)
        self.assertTrue(any('[lock] lost' in m for _c, m in posts), posts)
        self.assertEqual(ran_scripts, ['engine', 'trade_handoff_builder'])
        self.assertEqual(dashboard_calls, [])
        # 1 success (signals) + 1 raise (handoff, attempt) + 1 raise
        # (handoff, retry) — exactly one retry before giving up.
        self.assertEqual(len(renew_calls), 3)


class TestRunStepIsCapped(unittest.TestCase):
    """run_step wraps the child in a MemoryMax scope when one is available."""

    def setUp(self):
        from lib import capped_spawn as cs
        self.cs = cs
        self._orig_resolve = po._resolve_script

    def tearDown(self):
        po._resolve_script = self._orig_resolve
        # Deviation from the brief's literal `self.cs._reset()`: that leaves
        # availability unresolved (None), so the NEXT run_step call in this
        # process — including in another test module sharing this global —
        # re-probes for real. Pin back to the module's hermetic default
        # (available=False) instead; see setUpModule's comment.
        self.cs._reset(available=False)

    def test_argv_is_wrapped_when_scopes_are_available(self):
        seen = {}
        self.cs._reset(available=True)
        po._resolve_script = lambda script, run_date: (['true'], 5)

        real_popen = po.subprocess.Popen

        def _spy(cmd, **kw):
            seen['cmd'] = list(cmd)
            return real_popen(['true'], **kw)

        po.subprocess.Popen = _spy
        try:
            po.run_step('engine', DATE, dict(os.environ))
        finally:
            po.subprocess.Popen = real_popen
        self.assertEqual(seen['cmd'][:6],
                         ['systemd-run', '--scope', '--collect', '--quiet',
                          '-p', 'MemoryMax=4500M'])
        self.assertEqual(seen['cmd'][-1], 'true')

    def test_argv_is_untouched_when_scopes_are_unavailable(self):
        seen = {}
        self.cs._reset(available=False)
        po._resolve_script = lambda script, run_date: (['true'], 5)

        real_popen = po.subprocess.Popen

        def _spy(cmd, **kw):
            seen['cmd'] = list(cmd)
            return real_popen(['true'], **kw)

        po.subprocess.Popen = _spy
        try:
            ok, rc = po.run_step('engine', DATE, dict(os.environ))
        finally:
            po.subprocess.Popen = real_popen
        self.assertEqual(seen['cmd'], ['true'])
        self.assertTrue(ok)


if __name__ == '__main__':
    unittest.main()
