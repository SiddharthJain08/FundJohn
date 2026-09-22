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
    """List the live `proc:<host>:<pid>` heartbeats (QD E3).

    Diagnostic, not a gate: PASS with the roster, WARN only when an entry
    names a process that is no longer alive on this host (a writer that
    died between refreshes leaves a ghost until its TTL expires — worth
    seeing, never worth failing a maintenance run over). `requires=['fs']`
    because the runner has no 'redis' dep key (see registry.py's docstring);
    Redis reachability is handled here instead, via SKIP.
    """
    import socket
    try:
        import redis as _redis
        r = _redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379'),
                            socket_connect_timeout=2, decode_responses=True)
        keys = sorted(r.scan_iter('proc:*', count=100))
    except Exception as e:
        return Status.SKIP, f'redis unreachable ({type(e).__name__}) — no proc registry'
    if not keys:
        return Status.PASS, 'no live process heartbeats'
    me = socket.gethostname()
    lines, ghosts = [], []
    for k in keys[:20]:
        h = r.hgetall(k) or {}
        lines.append(f"{h.get('step') or '?'}@{h.get('pid') or '?'}"
                     f"({h.get('rss_mb') or '?'}MB)")
        if h.get('host') == me:
            try:
                os.kill(int(h.get('pid', 0)), 0)
            except PermissionError:
                pass
            except Exception:
                ghosts.append(k)
    detail = f'{len(keys)} live: ' + ', '.join(lines)
    if ghosts:
        return Status.WARN, (detail + f' | {len(ghosts)} ghost entr'
                             f'{"y" if len(ghosts) == 1 else "ies"}: {", ".join(ghosts[:3])}')[:200]
    return Status.PASS, detail[:200]
