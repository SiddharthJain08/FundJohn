"""Ledoit-Wolf (constant-correlation target) shrinkage — pure, no DB, no I/O.

Thin wrapper around pypfopt.risk_models.CovarianceShrinkage (pyportfolioopt
1.6.0, the version installed on this box — verified against
site-packages/pypfopt/risk_models.py). CovarianceShrinkage.__init__ sets
`self.delta = None`; whichever `.ledoit_wolf(shrinkage_target=...)` variant
runs reassigns `self.delta` to the fitted shrinkage intensity before
`_format_and_annualize` returns the shrunk covariance. That attribute —
`.delta` — is what this module calls `delta_hat` / `gamma`.

Task: docs/.superpowers/sdd/2026-08-24-five-repo-adoptions/task-P1-brief.md
"""
from __future__ import annotations

from typing import Optional

MIN_ROWS = 40
MIN_COLS = 3


def _clean_panel(panel: "pd.DataFrame"):
    """Drop all-NaN columns. Returns None if the result has < MIN_COLS
    columns or < MIN_ROWS rows (too thin to fit a shrinkage estimator)."""
    p = panel.dropna(axis=1, how='all')
    if p.shape[1] < MIN_COLS or p.shape[0] < MIN_ROWS:
        return None
    return p


def lw_corr(panel: "pd.DataFrame"):
    """Ledoit-Wolf (constant-correlation target) shrunk CORRELATION matrix
    from a dates x assets returns panel. Returns (corr_df, delta_hat); both
    None if the panel is too thin (see _clean_panel) or CovarianceShrinkage
    fails to fit.

    Uses pypfopt.risk_models.CovarianceShrinkage(panel, returns_data=True)
      .ledoit_wolf(shrinkage_target="constant_correlation")
    then pypfopt.risk_models.cov_to_corr.
    """
    p = _clean_panel(panel)
    if p is None:
        return None, None
    from pypfopt.risk_models import CovarianceShrinkage, cov_to_corr
    cs = CovarianceShrinkage(p, returns_data=True)
    shrunk_cov = cs.ledoit_wolf(shrinkage_target="constant_correlation")
    corr = cov_to_corr(shrunk_cov)
    return corr, float(cs.delta)


def lw_gamma(panel: "pd.DataFrame") -> Optional[float]:
    """Just the fitted shrinkage intensity delta_hat; None if the panel has
    < 3 columns or < 40 rows after dropping all-NaN columns (see
    _clean_panel), or if fitting otherwise fails to produce one."""
    p = _clean_panel(panel)
    if p is None:
        return None
    _corr, delta_hat = lw_corr(p)
    return delta_hat
