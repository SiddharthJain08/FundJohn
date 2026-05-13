"""Result types for system_checks."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class Status(str, Enum):
    PASS = 'PASS'
    WARN = 'WARN'
    FAIL = 'FAIL'
    SKIP = 'SKIP'   # dependency unavailable (e.g., POSTGRES_URI not set)
    ERROR = 'ERROR' # check itself raised an unhandled exception


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str = ''
    elapsed_ms: int = 0
    tags: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    skipped_dep: str | None = None  # which dep caused the skip
    error: str | None = None        # traceback summary on ERROR

    def to_dict(self) -> dict:
        d = asdict(self)
        d['status'] = self.status.value
        return d
