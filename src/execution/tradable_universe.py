"""Class-filtered reads of ``alpaca_tradable_universe``.

Since crypto engine Phase 3 the table carries crypto rows (INTERNAL symbol
``BTC-USD``, ``asset_class='crypto'``) next to the ``us_equity`` rows. Every
consumer that used to read "all active + tradable symbols" must now say which
classes it wants; the default is ``us_equity`` only, which is byte-identical to
the table before crypto rows existed. Rows with a NULL ``asset_class`` (legacy)
count as ``us_equity``.
"""
from __future__ import annotations

from typing import Iterable


def load_tradable_symbols(conn, *, classes: Iterable[str] = ('us_equity',)) -> set[str]:
    """Active + tradable symbols of the given asset classes.

    Raises on DB error — callers own their fail-open/closed policy."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT symbol FROM alpaca_tradable_universe
               WHERE status = 'active' AND tradable = TRUE
                 AND COALESCE(asset_class, 'us_equity') = ANY(%s)""",
            (list(classes),),
        )
        return {r[0] for r in cur.fetchall()}
