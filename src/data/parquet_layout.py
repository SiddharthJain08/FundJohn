"""Footer-only parquet layout evaluator (no data pages are ever read).

Answers: do the row groups of a date-keyed master prune on date? A group is
"tight" when its min..max date span is <= max_span_days. Used by the
prices_rowgroup_span system check so a ticker-contiguous / unsorted rewrite
cannot silently rot the layout again.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pyarrow.parquet as pq


def _coerce_date(v) -> date | None:
    """Row-group stat -> date, best effort. None when unparseable."""
    if v is None:
        return None
    if isinstance(v, bytes):
        v = v.decode('utf-8', 'replace')
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v[:19]).date()
        except ValueError:
            return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return None


def rowgroup_span_report(path: Path | str, date_col: str = 'date',
                         max_span_days: int = 60) -> dict:
    md = pq.ParquetFile(path).metadata
    idx = md.schema.names.index(date_col)
    spans: list[int] = []
    bounds: list[tuple[date, date]] = []
    missing = 0
    for i in range(md.num_row_groups):
        st = md.row_group(i).column(idx).statistics
        lo = hi = None
        if st is not None and st.has_min_max:
            lo, hi = _coerce_date(st.min), _coerce_date(st.max)
        if lo is None or hi is None:
            missing += 1
            continue
        spans.append((hi - lo).days)
        bounds.append((lo, hi))
    n_tight = sum(1 for s in spans if s <= max_span_days)
    ordered = sorted(spans)
    median = 0.0
    if ordered:
        m = len(ordered) // 2
        median = float(ordered[m] if len(ordered) % 2 else
                       (ordered[m - 1] + ordered[m]) / 2)
    monotone = all(bounds[i][1] <= bounds[i + 1][0]
                   for i in range(len(bounds) - 1))
    page_index = bool(md.num_row_groups) and all(
        md.row_group(0).column(c).has_column_index
        for c in range(md.num_columns))
    n = md.num_row_groups
    return {
        'n_row_groups': n,
        'n_tight': n_tight,
        'frac_tight': (n_tight / n) if n else 0.0,
        'median_span_days': median,
        'max_span_days_seen': max(spans) if spans else 0,
        'page_index': page_index,
        'monotone': monotone,
        'missing_stats': missing,
    }


def layout_verdict(report: dict, min_frac: float = 0.95) -> tuple[str, str]:
    """('FAIL'|'PASS', detail<=200). page_index is informational only."""
    detail = (f"{report['n_tight']}/{report['n_row_groups']} row groups tight "
              f"(frac={report['frac_tight']:.3f}), median_span="
              f"{report['median_span_days']:g}d, max_span="
              f"{report['max_span_days_seen']}d, monotone={report['monotone']}, "
              f"missing_stats={report['missing_stats']}, "
              f"page_index={report['page_index']}")
    if report['frac_tight'] < min_frac or report['missing_stats'] > 0:
        return 'FAIL', detail[:200]
    return 'PASS', detail[:200]
