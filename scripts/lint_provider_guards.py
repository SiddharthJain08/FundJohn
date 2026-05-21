"""SP-1 provider-guard lint. Fails on disallowed imports/references."""
import re
import sys
from pathlib import Path

ALLOWED_YFINANCE_PATHS = {
    'src/ingestion/cboe_vol_indices.py',
}

FORBIDDEN_PATTERNS = [
    (r'^[ \t]*(?:from\s+yfinance|import\s+yfinance)', 'yfinance', ALLOWED_YFINANCE_PATHS),
    (r'^[ \t]*(?:from\s+polygon|import\s+polygon\b)', 'polygon', set()),
    (r'\bMASSIVE_ACCESS_KEY_ID\b', 'MASSIVE_ACCESS_KEY_ID', set()),
    (r'\bMASSIVE_SECRET_KEY\b', 'MASSIVE_SECRET_KEY', set()),
    (r'\bOPTIONS_DATA_SOURCE\b', 'OPTIONS_DATA_SOURCE', set()),
]

# Skip dirs: git, node_modules, docs, worktrees (.claude/), auto-gen MCP tools (workspaces/), self
SKIP_PATHS = ('.git/', 'node_modules/', 'docs/', '.claude/', 'workspaces/', 'scripts/lint_provider_guards.py')


def scan(root: Path) -> int:
    violations = []
    for path in root.rglob('*'):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(rel.startswith(s) for s in SKIP_PATHS):
            continue
        if path.suffix not in ('.py', '.js', '.ts'):
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, PermissionError):
            continue
        for pattern, name, allowlist in FORBIDDEN_PATTERNS:
            for m in re.finditer(pattern, text, re.MULTILINE):
                if rel in allowlist:
                    continue
                line = text[:m.start()].count('\n') + 1
                violations.append(f'{rel}:{line}: forbidden {name} reference: {m.group()!r}')
    if violations:
        print('SP-1 provider lint failed:', file=sys.stderr)
        for v in violations:
            print(f'  {v}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(scan(Path(__file__).resolve().parent.parent))
