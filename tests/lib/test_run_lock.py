"""tests/lib/test_run_lock.py — one owned, renewed run lock (QD spec §5 E1).

No real Redis: FakeRedis below implements only the four calls run_lock uses.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from lib import run_lock  # noqa: E402


class FakeRedis:
    """Minimal in-memory stub: set(nx/ex), get, delete, expire."""

    def __init__(self, store=None):
        self.store = dict(store or {})
        self.ttls = {}
        self.calls = []

    def set(self, k, v, nx=False, ex=None):
        self.calls.append(('set', k, v, nx, ex))
        if nx and k in self.store:
            return None
        self.store[k] = v
        if ex is not None:
            self.ttls[k] = ex
        return True

    def get(self, k):
        return self.store.get(k)

    def delete(self, k):
        self.calls.append(('delete', k))
        self.store.pop(k, None)
        self.ttls.pop(k, None)

    def expire(self, k, ttl):
        self.calls.append(('expire', k, ttl))
        if k not in self.store:
            return False
        self.ttls[k] = ttl
        return True

    def eval(self, script, numkeys, key, expected, new, ttl):
        """Emulates run_lock._TAKEOVER_CAS_SCRIPT — the Lua GET+SET
        compare-and-swap used only for takeover:

            if redis.call('GET', KEYS[1]) == ARGV[1] then
                return redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
            else
                return nil
            end

        Writes `new` (with `ttl`) iff the key's CURRENT value still equals
        `expected` — the dead-PID value the caller observed via a prior GET.
        Otherwise leaves the key untouched and returns None, exactly like a
        real Redis EVAL of that script would.
        """
        self.calls.append(('eval', key, expected, new, ttl))
        if self.store.get(key) == expected:
            self.store[key] = new
            self.ttls[key] = int(ttl)
            return 'OK'
        return None


class FakeRedisVanishingKey(FakeRedis):
    """Simulates a key whose TTL lapses in the gap between our SET NX and
    our GET: the first NX sees the key as present (so it fails, as if some
    other holder were there), but a GET run right after finds it already
    gone. The retry SET NX inside acquire() must then succeed cleanly."""

    def __init__(self):
        super().__init__()
        self._set_nx_attempts = 0

    def set(self, k, v, nx=False, ex=None):
        self.calls.append(('set', k, v, nx, ex))
        if nx:
            self._set_nx_attempts += 1
            if self._set_nx_attempts == 1:
                return None  # pretend busy — the holder is about to vanish
        self.store[k] = v
        if ex is not None:
            self.ttls[k] = ex
        return True

    def get(self, k):
        if self._set_nx_attempts < 2:
            return None  # the transient holder is already gone
        return self.store.get(k)


class FakeRedisRenewRace(FakeRedis):
    """Simulates the key expiring under us, then another launcher's
    acquire() winning the SET NX race in the exact same gap before our
    renew() can re-take it — renew must fall through to "held by someone
    else" rather than clobbering that legitimate new owner."""

    def __init__(self, other_value):
        super().__init__()  # starts empty — our key has already expired
        self._other_value = other_value
        self._nx_attempted = False

    def set(self, k, v, nx=False, ex=None):
        self.calls.append(('set', k, v, nx, ex))
        if nx and not self._nx_attempted:
            self._nx_attempted = True
            # Another launcher's acquire() already grabbed it in this gap.
            self.store[k] = self._other_value
            self.ttls[k] = 9999
            return None  # our NX sees it as already present -> fails
        self.store[k] = v
        if ex is not None:
            self.ttls[k] = ex
        return True


class FakeRedisExpireMisses(FakeRedis):
    """expire() reports the key as gone regardless of prior state — models
    the TTL lapsing (or the key otherwise vanishing) strictly between our
    GET (which saw our own value) and our EXPIRE call. Pops the key from
    the store too, so the fake's own state stays honest with the fiction
    it is telling the caller."""

    def expire(self, k, ttl):
        self.calls.append(('expire', k, ttl))
        self.store.pop(k, None)
        self.ttls.pop(k, None)
        return False


class FakeRedisDeleteRaises(FakeRedis):
    """delete() blows up — models a Redis connection error on the way out.
    release() must swallow this, never propagate it."""

    def delete(self, k):
        self.calls.append(('delete', k))
        raise ConnectionError('redis unavailable')


DATE = '2026-09-14'
KEY = f'pipeline:run_lock:{DATE}'


class TestKeyAndValue(unittest.TestCase):
    def test_key_prefix_comes_from_the_shared_json_file(self):
        cfg = json.loads((ROOT / 'src' / 'lib' / 'run_lock_key.json').read_text())
        self.assertEqual(cfg['prefix'], 'pipeline:run_lock')
        self.assertEqual(run_lock.KEY_PREFIX, cfg['prefix'])
        self.assertEqual(run_lock.lock_key(DATE), KEY)

    def test_value_is_host_pid_start_iso_and_round_trips(self):
        v = run_lock.make_value(pid=4242, host='vps1', started_at='2026-09-14T10:00:00+00:00')
        self.assertEqual(v, 'vps1:4242:2026-09-14T10:00:00+00:00')
        self.assertEqual(run_lock.parse_value(v), ('vps1', 4242, '2026-09-14T10:00:00+00:00'))

    def test_parse_value_rejects_garbage(self):
        self.assertIsNone(run_lock.parse_value(None))
        self.assertIsNone(run_lock.parse_value(''))
        self.assertIsNone(run_lock.parse_value('1'))
        self.assertIsNone(run_lock.parse_value('host:notapid:2026-09-14T10:00:00+00:00'))

    def test_ttl_is_step_timeout_plus_slack_with_a_floor(self):
        self.assertEqual(run_lock.ttl_for(9000), 9120)
        self.assertEqual(run_lock.ttl_for(300), 420)
        self.assertEqual(run_lock.ttl_for(1), run_lock.MIN_TTL_SECONDS)

    def test_lock_busy_rc_is_75(self):
        self.assertEqual(run_lock.LOCK_BUSY_RC, 75)


class TestAcquire(unittest.TestCase):
    def test_acquires_a_free_lock_and_stores_the_owned_value(self):
        r = FakeRedis()
        ok, value = run_lock.acquire(r, DATE, 420, value='vps1:111:T0')
        self.assertTrue(ok)
        self.assertEqual(value, 'vps1:111:T0')
        self.assertEqual(r.store[KEY], 'vps1:111:T0')
        self.assertEqual(r.ttls[KEY], 420)

    def test_refuses_when_held_by_a_live_pid_and_returns_the_holder(self):
        held = f'vps1:{os.getpid()}:T0'
        r = FakeRedis({KEY: held})
        ok, holder = run_lock.acquire(r, DATE, 420, value='vps1:999:T1', host='vps1')
        self.assertFalse(ok)
        self.assertEqual(holder, held)
        self.assertEqual(r.store[KEY], held)

    def test_takes_over_when_the_holder_pid_is_dead_on_this_host(self):
        logs = []
        r = FakeRedis({KEY: 'vps1:999999:T0'})
        ok, value = run_lock.acquire(r, DATE, 420, value='vps1:111:T1',
                                     host='vps1', log=logs.append)
        self.assertTrue(ok)
        self.assertEqual(r.store[KEY], 'vps1:111:T1')
        self.assertTrue(any('stale lock from pid 999999' in m for m in logs), logs)

    def test_never_takes_over_a_lock_held_by_another_host(self):
        r = FakeRedis({KEY: 'otherbox:999999:T0'})
        ok, holder = run_lock.acquire(r, DATE, 420, value='vps1:111:T1', host='vps1')
        self.assertFalse(ok)
        self.assertEqual(holder, 'otherbox:999999:T0')

    def test_refuses_on_an_unparseable_holder_rather_than_stealing(self):
        r = FakeRedis({KEY: '1'})
        ok, holder = run_lock.acquire(r, DATE, 420, value='vps1:111:T1', host='vps1')
        self.assertFalse(ok)
        self.assertEqual(holder, '1')

    def test_retries_the_nx_once_when_the_holder_vanishes_before_our_get(self):
        # Finding 4: SET NX finds the key present (busy) but a same-instant
        # TTL lapse means our GET sees it already gone. That is "free", not
        # "busy" — acquire() must retry the NX once instead of reporting a
        # live holder that no longer exists.
        r = FakeRedisVanishingKey()
        ok, value = run_lock.acquire(r, DATE, 420, value='vps1:111:T1', host='vps1')
        self.assertTrue(ok)
        self.assertEqual(value, 'vps1:111:T1')
        self.assertEqual(r.store[KEY], 'vps1:111:T1')
        nx_sets = [c for c in r.calls if c[0] == 'set' and c[3] is True]
        self.assertEqual(len(nx_sets), 2)  # the original attempt + one retry


class TestTakeoverCAS(unittest.TestCase):
    """Finding 2: takeover is an atomic Lua compare-and-swap, not a blind
    overwrite, so two same-host racers contending for one dead-PID lock can
    never both win."""

    def test_two_same_host_takeovers_through_acquire_yield_exactly_one_winner(self):
        # Racer 1 takes over for real through acquire(). Its value carries
        # THIS test process's own pid, so racer 2's later GET sees a
        # genuinely live holder and refuses via the ordinary liveness
        # check — never reaching (or needing) its own takeover attempt.
        # One winner, through the real public API.
        r = FakeRedis({KEY: 'vps1:999999:T0'})
        my_value = f'vps1:{os.getpid()}:T1'
        ok1, value1 = run_lock.acquire(r, DATE, 420, value=my_value, host='vps1')
        self.assertTrue(ok1)
        self.assertEqual(r.store[KEY], my_value)

        ok2, holder2 = run_lock.acquire(r, DATE, 420, value='vps1:222:T2', host='vps1')
        self.assertFalse(ok2)
        self.assertEqual(holder2, my_value)
        self.assertEqual(r.store[KEY], my_value)  # racer 1's write stands

    def test_second_racers_cas_on_the_original_stale_value_is_rejected(self):
        # The actual race window findings 2 is about: two racers who BOTH
        # observed the same dead-PID value via GET before either one's CAS
        # committed. A single-threaded fake can't produce that interleaving
        # through two sequential acquire() calls (the second call's own GET
        # would just see racer 1's fresh value), so this drives the CAS
        # primitive directly with the shared stale `expected` both racers
        # would have used — the exact mechanism acquire() calls into.
        stale = 'vps1:999999:T0'
        r = FakeRedis({KEY: stale})
        ttl = 420

        ok1, value1 = run_lock.acquire(r, DATE, ttl, value='vps1:111:T1', host='vps1')
        self.assertTrue(ok1)
        self.assertEqual(r.store[KEY], 'vps1:111:T1')

        # Racer 2's CAS, conditioned on the same stale value both racers
        # observed, now runs against a store racer 1 already moved on.
        result2 = r.eval(run_lock._TAKEOVER_CAS_SCRIPT, 1, KEY, stale, 'vps1:222:T2', ttl)
        self.assertFalse(bool(result2))
        self.assertEqual(r.store[KEY], 'vps1:111:T1')  # untouched by the loser


class TestRenewAndRelease(unittest.TestCase):
    def test_renew_extends_only_our_own_lock(self):
        r = FakeRedis({KEY: 'vps1:111:T0'})
        self.assertTrue(run_lock.renew(r, DATE, 'vps1:111:T0', 9120))
        self.assertEqual(r.ttls[KEY], 9120)

    def test_renew_refuses_when_another_owner_took_over(self):
        logs = []
        r = FakeRedis({KEY: 'vps1:222:T1'})
        self.assertFalse(run_lock.renew(r, DATE, 'vps1:111:T0', 9120, log=logs.append))
        self.assertEqual(r.ttls, {})
        self.assertTrue(any('held by vps1:222:T1' in m for m in logs), logs)

    def test_renew_re_takes_a_lock_that_expired_under_us(self):
        r = FakeRedis()
        self.assertTrue(run_lock.renew(r, DATE, 'vps1:111:T0', 9120))
        self.assertEqual(r.store[KEY], 'vps1:111:T0')

    def test_renew_refuses_when_the_key_expired_and_someone_else_won_the_retake(self):
        # Finding 1(a): the expired-under-us re-take must be an NX, not an
        # unconditional SET — if another launcher's acquire() legitimately
        # grabbed the key in the same gap, renew() must fall through to
        # "held by someone else" rather than clobbering it.
        logs = []
        r = FakeRedisRenewRace('vps1:222:T9')
        ok = run_lock.renew(r, DATE, 'vps1:111:T0', 9120, log=logs.append)
        self.assertFalse(ok)
        self.assertEqual(r.store[KEY], 'vps1:222:T9')  # the real owner's write stands
        self.assertTrue(any('held by vps1:222:T9' in m for m in logs), logs)

    def test_renew_returns_false_when_expire_finds_the_key_gone(self):
        # Finding 1(b): renew must honour EXPIRE's real return value rather
        # than assuming success — if the key vanished strictly between our
        # GET (which matched our own value) and our EXPIRE call, renew must
        # report False, not True.
        r = FakeRedisExpireMisses({KEY: 'vps1:111:T0'})
        self.assertFalse(run_lock.renew(r, DATE, 'vps1:111:T0', 9120))

    def test_release_deletes_only_our_own_lock(self):
        r = FakeRedis({KEY: 'vps1:111:T0'})
        self.assertTrue(run_lock.release(r, DATE, 'vps1:111:T0'))
        self.assertNotIn(KEY, r.store)

    def test_release_never_deletes_a_lock_someone_else_now_owns(self):
        logs = []
        r = FakeRedis({KEY: 'vps1:222:T1'})
        self.assertFalse(run_lock.release(r, DATE, 'vps1:111:T0', log=logs.append))
        self.assertEqual(r.store[KEY], 'vps1:222:T1')
        self.assertNotIn(('delete', KEY), r.calls)

    def test_release_of_an_absent_lock_is_a_no_op(self):
        r = FakeRedis()
        self.assertFalse(run_lock.release(r, DATE, 'vps1:111:T0'))

    def test_release_with_no_value_is_a_no_op(self):
        r = FakeRedis({KEY: 'vps1:111:T0'})
        self.assertFalse(run_lock.release(r, DATE, None))
        self.assertIn(KEY, r.store)

    def test_release_swallows_a_delete_error_and_returns_false(self):
        # Finding 3: release() runs on the way out (typically a `finally`)
        # and must never raise — a network hiccup here must not mask or
        # replace the run's real exit status.
        logs = []
        r = FakeRedisDeleteRaises({KEY: 'vps1:111:T0'})
        result = run_lock.release(r, DATE, 'vps1:111:T0', log=logs.append)
        self.assertFalse(result)
        self.assertTrue(any('release of' in m for m in logs), logs)


if __name__ == '__main__':
    unittest.main()
