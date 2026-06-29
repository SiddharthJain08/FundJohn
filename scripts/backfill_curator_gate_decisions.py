#!/usr/bin/env python3
"""
One-time: repair the curator gate_decisions inversion (W4-3).
implementable_candidate is a PROMOTED bucket but was recorded outcome='reject'.
Flip those to 'pass' so paper_hit_rate_funnel curator metrics are correct.
Idempotent. APPLY-only — run explicitly at the gated deploy.

SCHEMA NOTE (confirmed from 033_paper_gate_decisions.sql): paper_gate_decisions has NO
'predicted_bucket' column. The bucket is stored in reason_code as 'bucket_<bucket_name>'.
So this backfill targets reason_code='bucket_implementable_candidate'.
"""
import os
import psycopg2

SQL = (
    "UPDATE paper_gate_decisions SET outcome='pass' "
    "WHERE gate_name='curator' AND outcome='reject' AND reason_code='bucket_implementable_candidate'"
)


def main():
    dsn = os.environ["POSTGRES_URI"]
    with psycopg2.connect(dsn) as c, c.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM paper_gate_decisions "
            "WHERE gate_name='curator' AND outcome='reject' AND reason_code='bucket_implementable_candidate'"
        )
        n = cur.fetchone()[0]
        print(f"[backfill] {n} curator/implementable_candidate rows currently mislabeled 'reject'")
        cur.execute(SQL)
        print(f"[backfill] flipped {cur.rowcount} -> 'pass'")
        c.commit()


if __name__ == "__main__":
    main()
