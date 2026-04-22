# Provider-specific historical-backfill adapters.
#
# Each module exports:
#     def backfill(column_name: str, from_date: date, to_date: date) -> int:
# returning the number of rows written to master parquets / DB tables.
# Raise on unrecoverable errors; the caller marks backfill_status='failed'.
