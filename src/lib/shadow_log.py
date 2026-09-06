"""Append-only sink for one-line shadow diagnostics that must survive the
daily-cycle step log's 4,000-character tail (src/agent/graphs/daily_cycle_node.js).
record(name, line) appends '<UTC ISO timestamp> <line>\n' to logs/<name>.log
(dir overridable with OPENCLAW_SHADOW_LOG_DIR). Never raises."""
from __future__ import annotations
import datetime as dt, logging, os
from pathlib import Path
log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
DIR_ENV = 'OPENCLAW_SHADOW_LOG_DIR'

def shadow_dir() -> Path:
    return Path(os.environ.get(DIR_ENV) or (ROOT / 'logs'))

def record(name: str, line: str) -> Path | None:
    try:
        d = shadow_dir(); d.mkdir(parents=True, exist_ok=True)
        p = d / f'{name}.log'
        stamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        with open(p, 'a') as fh:
            fh.write(f'{stamp} {line.rstrip()}\n')
        return p
    except Exception as exc:  # noqa: BLE001 — a diagnostic sink must never break the caller
        log.debug('shadow_log: could not record %s: %s', name, exc)
        return None
