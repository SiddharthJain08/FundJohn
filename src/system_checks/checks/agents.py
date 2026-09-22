"""Agent checks — LLM CLI reachability + confirmer path."""
from __future__ import annotations

import os
import subprocess

from ..registry import check
from ..types import Status

CLAUDE_BIN = '/usr/local/bin/claude-bin'


@check(name='claude_bin_responds', tags=['agents'], requires=['llm'])
def _claude_bin_responds():
    """A tiny --print roundtrip exits cleanly. Regression for the 2026-05-13 confirmer
    --max-tokens flag change. Budget is sized for one tiny call: Sonnet output ~$15/M,
    cache-overhead alone is ~$0.05, so $0.30 is generous enough to complete but small
    enough to detect runaway loops."""
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, '--print', '--output-format', 'json',
             '--model', 'sonnet', '--max-budget-usd', '0.30'],
            input=b'Reply with the literal JSON {"ok": true}. Nothing else.',
            capture_output=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return Status.FAIL, 'timeout after 60s'
    import json
    try:
        out = json.loads(proc.stdout.decode()) if proc.stdout else {}
    except json.JSONDecodeError:
        return Status.FAIL, f'response not JSON (rc={proc.returncode})'
    # rc may be 1 on budget-cap hit; treat that as "CLI reachable".
    if out.get('subtype', '').startswith('error_max_budget'):
        return Status.PASS, 'CLI reachable (budget cap as expected)'
    if proc.returncode != 0:
        return Status.FAIL, f'exit {proc.returncode}: {proc.stderr[:120].decode("utf-8", "replace")}'
    if out.get('is_error'):
        return Status.FAIL, f'CLI error: {out.get("subtype")}'
    return Status.PASS, f'CLI responded in {out.get("duration_ms", "?")}ms'


@check(name='discord_bot_token_valid', tags=['agents'], requires=['discord'])
def _discord_bot_token_valid():
    """DISCORD_BOT_TOKEN authenticates and the bot is in at least one guild.
    /users/@me/guilds is the right endpoint for bot tokens (vs /users/@me which
    requires a user OAuth scope the bot doesn't have)."""
    import urllib.request
    import urllib.error
    import json
    # Discord rejects requests without a UA header (returns 403). Match the
    # alpaca_executor's working pattern.
    req = urllib.request.Request(
        'https://discord.com/api/v10/users/@me/guilds',
        headers={
            'Authorization': f'Bot {os.environ["DISCORD_BOT_TOKEN"]}',
            'User-Agent': 'OpenClawSystemChecks/1.0 (+https://github.com/SiddharthJain08/FundJohn)',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            guilds = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return Status.FAIL, f'HTTP {e.code} {e.reason}'
    except Exception as e:
        return Status.FAIL, f'{type(e).__name__}: {e}'
    if not isinstance(guilds, list):
        return Status.FAIL, f'unexpected response shape: {type(guilds).__name__}'
    if not guilds:
        return Status.WARN, 'authed but bot is in 0 guilds'
    return Status.PASS, f'authed; bot is in {len(guilds)} guild(s)'


@check(name='proc_registry', tags=['agents'], requires=['fs'])
def _proc_registry():
    """List the live `proc:<host>:<pid>` heartbeats (QD E3; ruled semantics,
    fix round 1).

    SKIPs on ANY Redis error — connection refused up front, or a connection
    that dies mid-read (a firewalled/restarting Redis can drop on the Nth
    key just as easily as the first: the try wraps the WHOLE read loop, not
    just the connect+scan). A heartbeat registry being briefly unreachable
    is diagnostics-unavailable, not a system_checks-worthy ERROR. WARN only
    when a same-host entry names a PID that's no longer alive (a writer
    that died between refreshes leaves a ghost until its TTL expires —
    worth seeing, never worth failing a maintenance run over; a MISSING pid
    field is treated the same way — `run_lock.pid_alive` returns False for
    an unparseable/absent pid, so it's "not live" too, not a crash).
    `requires=['fs']` because the runner has no 'redis' dep key (see
    registry.py's docstring); Redis reachability is handled here instead,
    via SKIP.
    """
    import itertools
    import socket
    import time as _time
    from datetime import datetime

    # Imported ABOVE the try (same rule doctor.py's check_orchestrator_lock
    # follows for KEY_PREFIX): an ImportError here is a real bug in this
    # check, not a Redis problem, and must not be mislabeled SKIP.
    from lib.run_lock import pid_alive

    MAX_ENTRIES = 20
    me = socket.gethostname()
    now = _time.time()
    try:
        import redis as _redis
        r = _redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379'),
                            socket_connect_timeout=2, socket_timeout=2,
                            decode_responses=True)
        lines, ghosts, n_seen = [], [], 0
        # Cap WHILE iterating — scan_iter is a cursor-based generator;
        # islice never pulls more than MAX_ENTRIES even against a registry
        # with thousands of live keys (no `sorted(list(...))` materialising
        # the whole scan first).
        for k in itertools.islice(r.scan_iter('proc:*', count=100), MAX_ENTRIES):
            n_seen += 1
            h = r.hgetall(k) or {}
            pid_raw = h.get('pid')
            age_s = '?'
            updated_at = h.get('updated_at')
            if updated_at:
                try:
                    age_s = int(now - datetime.fromisoformat(
                        updated_at.replace('Z', '+00:00')).timestamp())
                except Exception:
                    age_s = '?'
            lines.append(f"{h.get('step') or '?'}@{pid_raw or '?'}"
                         f"({h.get('rss_mb') or '?'}MB,age={age_s}s)")
            if h.get('host') == me and not pid_alive(pid_raw):
                ghosts.append(str(k))
    except Exception as e:
        return Status.SKIP, f'redis error ({type(e).__name__}) — no proc registry'
    if not lines:
        return Status.PASS, 'no live process heartbeats'
    detail = f'{n_seen} shown (cap {MAX_ENTRIES}): ' + ', '.join(lines)
    if ghosts:
        return Status.WARN, (detail + f' | {len(ghosts)} ghost entr'
                             f'{"y" if len(ghosts) == 1 else "ies"}: {", ".join(ghosts[:3])}')[:200]
    return Status.PASS, detail[:200]
