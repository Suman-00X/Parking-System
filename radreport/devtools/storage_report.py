"""Prints table health and what compression saves, for ops; run as the database owner so every row is counted.

Order: table vacuum and bloat state (table_stats) -> stored against raw bytes for each compressed
column (compression_ratio).

    python -m radreport.devtools.storage_report --url postgresql+psycopg://owner@localhost/radreport
"""

from __future__ import annotations

import argparse

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from radreport.db.table_health import COMPRESSED_COLUMNS, compression_ratio, table_stats


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True)
    args = parser.parse_args(argv)
    with Session(create_engine(args.url)) as session:
        print(f"{'table':<32} {'live':>10} {'dead':>8} {'dead %':>7} {'size MB':>9}  last autovacuum")
        for t in table_stats(session):
            print(f"{t.table:<32} {t.live_rows:>10} {t.dead_rows:>8} {t.dead_ratio:>7.1%} {t.total_bytes / 1e6:>9.2f}  {t.last_autovacuum or 'never'}{'  BLOATED' if t.bloated else ''}")
        print()
        for table, column in COMPRESSED_COLUMNS:
            r = compression_ratio(session, table, column)
            if r["raw_bytes"]:
                print(f"{table}.{column:<28} raw {r['raw_bytes'] / 1e6:8.2f} MB  stored {r['stored_bytes'] / 1e6:8.2f} MB  saved {r['saved']:6.1%}  {','.join(r['methods']) or '-'}")


if __name__ == "__main__":
    main()
