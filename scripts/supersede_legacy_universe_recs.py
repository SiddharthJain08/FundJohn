#!/usr/bin/env python3
"""One-shot: tag the 58 stale 2026-05-25 legacy Phase-C recommendation rows
as superseded (rationale append — NO deletion, spec §5). Idempotent."""
from __future__ import annotations
import os
import sys

import psycopg2

TAG = ' [superseded-by:sp7b-tier-ladder 2026-06-06]'


def main() -> int:
    pg = psycopg2.connect(os.environ['POSTGRES_URI'])
    with pg.cursor() as cur:
        cur.execute("""
            UPDATE strategy_universe_recommendations
               SET rationale = COALESCE(rationale, '') || %s
             WHERE recommended_at::date = '2026-05-25'
               AND approved IS NULL AND adopted = FALSE
               AND COALESCE(rationale, '') NOT LIKE %s""",
            (TAG, '%superseded-by:sp7b-tier-ladder%'))
        print(f'[supersede] tagged {cur.rowcount} legacy rows')
    pg.commit()
    pg.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
