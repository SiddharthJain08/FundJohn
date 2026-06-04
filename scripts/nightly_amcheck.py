#!/usr/bin/env python3
"""Nightly btree-integrity sweep (LRN-20260604-003): run the
btree_index_integrity system_check and ALERT #data-alerts on anything
non-PASS. Detection only — repair stays operator-gated.

Installed as openclaw-amcheck.{service,timer}, daily 05:37 UTC.
Exit: 0 PASS / 1 WARN / 2 FAIL or ERROR (mirrors system_checks runner).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / 'src')):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load_env():
    # Best-effort: under systemd the unit injects EnvironmentFile=.env as root
    # before dropping to claudebot, who cannot read /root/openclaw/.env itself.
    try:
        for line in open(ROOT / '.env'):
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


def _post_discord(text: str) -> None:
    try:
        import requests
        import psycopg2
        with psycopg2.connect(os.environ['POSTGRES_URI']) as c:
            with c.cursor() as cur:
                cur.execute("SELECT webhook_urls->>'data-alerts' FROM agent_registry WHERE id='botjohn'")
                row = cur.fetchone()
        url = row and row[0]
        if url:
            requests.post(url, json={'content': text[:1900]},
                          headers={'User-Agent': 'openclaw-amcheck/1.0'}, timeout=10)
    except Exception as e:
        print(f'[amcheck] discord post failed: {e}')


def main() -> int:
    _load_env()
    from system_checks.registry import get
    from system_checks.types import Status
    import system_checks.checks  # noqa: F401

    status, detail = get('btree_index_integrity')['fn']()
    print(f'btree_index_integrity: {status.name} — {detail}')
    if status is Status.PASS:
        return 0
    icon = '🟡' if status is Status.WARN else '🔴'
    _post_discord(f'{icon} **Nightly amcheck: btree_index_integrity {status.name}**\n{detail}')
    return 1 if status is Status.WARN else 2


if __name__ == '__main__':
    sys.exit(main())
