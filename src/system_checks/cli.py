"""CLI entry point: `python3 -m system_checks [...]`.

Exit codes mirror src/maintenance/doctor.py:
    0 — all checks PASS or SKIP
    1 — at least one WARN, none FAIL/ERROR
    2 — at least one FAIL or ERROR
"""
from __future__ import annotations

import argparse
import json
import sys

from . import run_all, run_one, all_checks, summarize
from .types import Status


# Color codes work over PTYs; auto-disabled when not a terminal.
def _color(code: str, txt: str, enabled: bool) -> str:
    return f'\033[{code}m{txt}\033[0m' if enabled else txt


_STATUS_COLOR = {
    Status.PASS:  '32',  # green
    Status.WARN:  '33',  # yellow
    Status.FAIL:  '31',  # red
    Status.ERROR: '31',
    Status.SKIP:  '90',  # gray
}


def _format_human(results, use_color: bool) -> str:
    lines = []
    name_w = max((len(r.name) for r in results), default=10) + 2
    for r in results:
        emoji = {
            Status.PASS:  '✓',
            Status.WARN:  '!',
            Status.FAIL:  '✗',
            Status.ERROR: '✗',
            Status.SKIP:  '–',
        }[r.status]
        color = _STATUS_COLOR[r.status]
        status_txt = _color(color, f'{r.status.value:<5}', use_color)
        emoji_txt = _color(color, emoji, use_color)
        lines.append(
            f'  {emoji_txt} {r.name:<{name_w}}{status_txt}  '
            f'{r.elapsed_ms:>4}ms  {r.detail}'
        )
    summary = summarize(results)
    counts = ' '.join(
        f'{summary[s.value]} {s.value.lower()}'
        for s in Status if summary[s.value] > 0
    )
    lines.append('')
    lines.append(f'  {summary["total"]} check(s): {counts}')
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='system_checks',
        description='Runnable diagnostic probes for live OpenClaw state.')
    ap.add_argument('--check', dest='names', action='append', default=None,
                    help='run specific check by name (repeatable)')
    ap.add_argument('--tag', dest='tags', action='append', default=None,
                    help='only run checks carrying this tag (repeatable)')
    ap.add_argument('--list', action='store_true',
                    help='print registered checks and exit')
    ap.add_argument('--json', action='store_true',
                    help='emit JSON instead of human-readable output')
    ap.add_argument('--no-color', action='store_true')
    args = ap.parse_args(argv)

    reg = all_checks()
    if args.list:
        if args.json:
            payload = {n: {'tags': m['tags'], 'requires': m['requires']}
                       for n, m in reg.items()}
            json.dump(payload, sys.stdout, indent=2)
            print()
        else:
            for n in sorted(reg):
                m = reg[n]
                print(f'  {n}  tags={m["tags"]}  requires={m["requires"]}')
            print(f'\n  {len(reg)} check(s) registered')
        return 0

    if not reg:
        print('no checks registered', file=sys.stderr)
        return 2

    results = run_all(tags=args.tags, names=args.names)

    if args.json:
        json.dump({
            'summary': summarize(results),
            'results': [r.to_dict() for r in results],
        }, sys.stdout)
        print()
    else:
        use_color = sys.stdout.isatty() and not args.no_color
        print(_format_human(results, use_color))

    summary = summarize(results)
    if summary['FAIL'] or summary['ERROR']:
        return 2
    if summary['WARN']:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
