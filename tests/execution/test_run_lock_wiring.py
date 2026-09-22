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

# QD E2 hermeticity (fix round 2, task-4 review finding 1): run_step calls
# capped_spawn.wrap_capped() on every invocation. Its module-global
# `_STATE['available']` defaults to None (unresolved), and on an unresolved
# probe it shells out to a REAL `systemd-run`. Module-scoped setUpModule/
# tearDownModule used to pin this for the whole file, but that pinned
# process-global state for every test regardless of pytest invocation order
# and could not be composed with a real leak elsewhere in the same process
# (the review's finding 1 repro). The pin now lives in
# tests/execution/conftest.py as an autouse fixture covering the whole
# directory (OPENCLAW_STEP_MEMORY_MAX=0 + `_reset(available=False)` per
# test, every test). `TestRunStepIsCapped` below overrides both explicitly
# in its own setUp/tearDown to exercise the "available" branch.


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

    def eval(self, _script, _numkeys, key, expected, new, ttl):
        """Emulates run_lock.py's takeover CAS (a Lua GET+SET in real Redis):
        writes `new` iff the key's CURRENT value still equals `expected`.
        NOT Python's builtin eval — this is the Redis client's EVAL command
        method name; no code is executed here (same fake as
        tests/lib/test_run_lock.py carries)."""
        if self.store.get(key) == expected:
            self.store[key] = new
            self.ttls[key] = int(ttl)
            return True
        return None


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


class TestRunStepHeartbeat(unittest.TestCase):
    """QD E3: run_step calls the optional `heartbeat` callback with the
    spawned child's pid and the captured start stamp, and swallows a
    raising heartbeat rather than letting it break the step — a heartbeat
    is diagnostics, never load-bearing for step success/failure."""

    def setUp(self):
        self._orig_resolve = po._resolve_script
        po._resolve_script = lambda script, run_date: (['true'], 5)

    def tearDown(self):
        po._resolve_script = self._orig_resolve

    def test_heartbeat_fires_with_the_child_pid_and_a_start_stamp(self):
        calls = []
        ok, rc = po.run_step('engine', DATE, dict(os.environ),
                             heartbeat=lambda pid, started_at: calls.append((pid, started_at)))
        self.assertTrue(ok)
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(len(calls), 1)
        pid, started_at = calls[0]
        self.assertIsInstance(pid, int)
        self.assertGreater(pid, 0)
        self.assertIsInstance(started_at, str)
        self.assertTrue(started_at)   # a real (non-empty) ISO stamp

    def test_a_raising_heartbeat_never_breaks_the_step(self):
        def _boom(pid, started_at):
            raise RuntimeError('redis down')

        ok, rc = po.run_step('engine', DATE, dict(os.environ), heartbeat=_boom)
        self.assertTrue(ok)
        self.assertEqual(rc, 0)

    def test_run_step_without_heartbeat_is_unchanged(self):
        ok, rc = po.run_step('engine', DATE, dict(os.environ))
        self.assertTrue(ok)
        self.assertEqual(rc, 0)


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
    """Controller ruling, wave-1 fix item 5 — SUPERSEDES the fix-round-1
    "zero lock interaction" rule tested here before.

    Fix round 1 had --force-resume skip the lock entirely. That left the
    COMMON case unlocked: the key is usually free, and
    scripts/redeploy_pipeline.py hardcodes --force-resume onto every intraday
    redeploy, so a redeploy could run `trade`/`alpaca` concurrently with the
    15:00 cycle — a double-submission shape.

    The ruling now is:
      * key FREE      → acquire normally (owned value, per-step renew,
                        value-checked release) — i.e. just fall through to
                        the same `acquire_lock()` every other run uses;
      * DEAD same-host holder → taken over by that same `acquire_lock()`;
      * LIVE holder   → run BESIDE it without touching the key at all (log +
                        post `[lock] --force-resume: running beside holder
                        <value>`). `LOCK_VALUE` stays unset in this case, so
                        every `release_lock()` call — including the top-level
                        `finally` — remains a no-op and the holder's own value
                        is never overwritten or deleted.
    """

    def setUp(self):
        po.LOCK_VALUE = None

    def tearDown(self):
        po.LOCK_VALUE = None

    def _run(self, r, snap=None):
        """Drive main() with --force-resume and a stubbed run_step.

        `snap`, when given, records the lock state as observed from INSIDE the
        first step — the only way to tell "acquired then released" from "never
        acquired", since both end with the key absent.
        """
        with _hermetic_main(r) as (posts, feed_msgs, alert_msgs, dashboard_calls):
            def _run_step(script, run_date, env, renew=None, **_kwargs):
                if snap is not None and 'key' not in snap:
                    snap['key'] = r.store.get(KEY)
                    snap['lock_value'] = po.LOCK_VALUE
                return (True, 0)
            po.run_step = _run_step
            rc = po.main(['--date', DATE, '--force-resume', '--steps', 'report'])
        return rc, posts, dashboard_calls

    def test_force_resume_with_a_free_key_acquires_an_owned_value(self):
        r = FakeRedis()
        snap = {}
        rc, posts, dashboard_calls = self._run(r, snap)
        self.assertEqual(rc, 0)
        # Mid-run: an owned value really was held (not the beside-holder path).
        self.assertIsNotNone(snap['key'])
        self.assertEqual(snap['key'], snap['lock_value'])
        self.assertEqual(run_lock.parse_value(snap['key'])[1], os.getpid())
        self.assertFalse(any('running beside holder' in m for _c, m in posts), posts)
        # …and released at the end of the run.
        self.assertNotIn(KEY, r.store)
        self.assertIsNone(po.LOCK_VALUE)
        self.assertEqual(dashboard_calls, [DATE])

    def test_force_resume_takes_over_a_dead_same_host_holder(self):
        r = FakeRedis({KEY: f'{po._HOST}:999999:T0'})   # our host, dead pid
        snap = {}
        rc, posts, dashboard_calls = self._run(r, snap)
        self.assertEqual(rc, 0)
        self.assertNotEqual(snap['key'], f'{po._HOST}:999999:T0')  # taken over
        self.assertEqual(snap['key'], snap['lock_value'])
        self.assertFalse(any('running beside holder' in m for _c, m in posts), posts)
        self.assertNotIn(KEY, r.store)                  # and released
        self.assertEqual(dashboard_calls, [DATE])

    def test_force_resume_with_live_holder_leaves_it_byte_identical(self):
        r = FakeRedis({KEY: 'otherbox:99999:T0'})   # some other live holder
        snap = {}
        rc, posts, dashboard_calls = self._run(r, snap)
        self.assertEqual(rc, 0)
        # Byte-identical — not overwritten, not deleted, at no point.
        self.assertEqual(snap['key'], 'otherbox:99999:T0')
        self.assertIsNone(snap['lock_value'])
        self.assertEqual(r.store[KEY], 'otherbox:99999:T0')
        self.assertIsNone(po.LOCK_VALUE)
        self.assertTrue(any('running beside holder' in m for _c, m in posts), posts)
        self.assertTrue(any('otherbox:99999:T0' in m for _c, m in posts), posts)
        self.assertEqual(dashboard_calls, [DATE])   # cycle still ran to completion

    def test_force_resume_beside_a_live_holder_still_runs_every_step(self):
        """With the REAL run_step (renew wiring live, `true` as the script):
        holding no lock must not abort the run at the first `_renew_or_lose`.
        """
        r = FakeRedis({KEY: 'otherbox:99999:T0'})
        ran_scripts = []
        with _hermetic_main(r) as (posts, feed_msgs, alert_msgs, dashboard_calls):
            po._resolve_script = lambda script, run_date: (['true'], 5)
            orig_run_step = po.run_step

            def _tracking_run_step(script, run_date, env, renew=None, **_kwargs):
                ran_scripts.append(script)
                return orig_run_step(script, run_date, env, renew=renew)

            po.run_step = _tracking_run_step
            rc = po.main(['--date', DATE, '--force-resume',
                          '--steps', 'signals,handoff'])
        self.assertEqual(rc, 0)
        self.assertEqual(ran_scripts, ['engine', 'trade_handoff_builder'])
        self.assertEqual(r.store[KEY], 'otherbox:99999:T0')
        self.assertIsNone(po.LOCK_VALUE)

    def test_without_force_resume_a_free_key_is_still_acquired(self):
        """Regression guard: the refusal path must only trigger on a real
        holder, and the ordinary (no-flag) run is unchanged by item 5."""
        r = FakeRedis()
        snap = {}
        with _hermetic_main(r) as (posts, feed_msgs, alert_msgs, dashboard_calls):
            def _run_step(script, run_date, env, renew=None, **_kwargs):
                snap.setdefault('key', r.store.get(KEY))
                return (True, 0)
            po.run_step = _run_step
            rc = po.main(['--date', DATE, '--steps', 'report'])
        self.assertEqual(rc, 0)
        self.assertIsNotNone(snap['key'])
        self.assertNotIn(KEY, r.store)
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

                def _tracking_run_step(script, run_date, env, renew=None, **_kwargs):
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

                def _tracking_run_step(script, run_date, env, renew=None, **_kwargs):
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
    """run_step wraps the child in a MemoryMax scope when one is available.

    Fix round 2 (task-4 review finding 1): this class pins availability
    directly via `_reset(available=...)`, which resolves `_STATE['available']`
    WITHOUT ever calling the probe — so `capped_spawn.subprocess.run` must
    never be invoked by any test here. setUp patches it to raise if it is,
    as a hard guard (belt and suspenders on top of the conftest-level
    counting proxy, which only asserts at the end of every test in the
    directory). This class also needs the REAL default cap decision
    (4500M), not the conftest's blanket OPENCLAW_STEP_MEMORY_MAX=0 override
    for the rest of the directory — setUp clears it for the duration of
    each test and tearDown restores whatever conftest's monkeypatch had set."""

    def setUp(self):
        from lib import capped_spawn as cs
        self.cs = cs
        self._orig_resolve = po._resolve_script
        self._old_cap_env = os.environ.get('OPENCLAW_STEP_MEMORY_MAX')
        os.environ.pop('OPENCLAW_STEP_MEMORY_MAX', None)
        self._real_run_patcher = mock.patch.object(
            cs.subprocess, 'run',
            side_effect=AssertionError(
                'capped_spawn._real_probe() must not run in '
                'TestRunStepIsCapped — availability is always pinned'))
        self._real_run_patcher.start()
        # Per the review ruling: pin available=True here (each test method
        # re-pins to whatever it actually needs as its first statement, so
        # this is overwritten immediately by every test below — but it
        # matches the ruling's literal setUp contract and, either way,
        # never touches the probe).
        self.cs._reset(available=True)

    def tearDown(self):
        self._real_run_patcher.stop()
        po._resolve_script = self._orig_resolve
        if self._old_cap_env is None:
            os.environ.pop('OPENCLAW_STEP_MEMORY_MAX', None)
        else:
            os.environ['OPENCLAW_STEP_MEMORY_MAX'] = self._old_cap_env
        # Deviation from the brief's literal `self.cs._reset()`: that leaves
        # availability unresolved (None). Pin back to False instead — the
        # conftest fixture's own teardown (`_reset()`, unpinned) runs AFTER
        # this tearDown and re-pins False again before the next test's
        # setup, so this is redundant-but-harmless defense in depth, not
        # load-bearing on its own.
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
