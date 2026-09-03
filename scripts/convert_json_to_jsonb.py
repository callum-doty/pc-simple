"""
Convert the documents JSON columns from ``json`` to ``jsonb``.

Deliberately NOT an Alembic migration. It was one, and it failed on deploy:
``ALTER TABLE ... ALTER COLUMN TYPE`` needs an ACCESS EXCLUSIVE lock, which
conflicts with every other lock including the ACCESS SHARE a plain SELECT
takes. The web instance, the Celery worker and the ingest cron are all holding
something, so the lock never comes and the statement times out.

Worse, Render's buildCommand has no ``set -e``: alembic exited non-zero, the
build carried on, and code that assumed the conversion had happened went live
against unconverted columns.

So this is an operator-run script for a maintenance window, and the metrics
layer no longer depends on it — ``services/metrics/jsonb.py`` detects the
column type at runtime and casts only while it must. Running this makes the
dashboard faster; not running it leaves it correct but slower.

WHY IT IS WORTH RUNNING

  * ``json -> jsonb`` re-parses the whole document for every row of every
    query that needs jsonb semantics.
  * A cast makes any index on the column unusable, so the GIN index on
    ``keywords`` can never apply while the cast is in place.
  * ``models/document.py`` declares these columns JSONB. Right now that
    declaration is false.

THIS CAUSES AN OUTAGE. PLAN FOR ONE.

  An earlier version of this file claimed the web service could keep serving
  during the conversion. That was wrong, and running it took the app down.

  Two things make it unavoidable:

  * ``ALTER TABLE ... ALTER COLUMN TYPE`` holds ACCESS EXCLUSIVE for the whole
    rewrite, not just to start it. Every query against ``documents`` blocks
    for the duration — minutes on a large table.
  * A *pending* ACCESS EXCLUSIVE request also blocks every new reader that
    queues behind it. So even the retry attempts stall traffic, whether or not
    they succeed.

  BEFORE RUNNING:

    1. Scale the web service to 0, or accept that it will be unavailable.
    2. Scale ``celery-worker`` to 0.
    3. Suspend the ``dropbox-ingest`` cron.

  AFTER:

    4. Restore all three.
    5. Restart the web service so it re-detects the column types — the
       detection in services/metrics/jsonb.py is cached per process, so a
       running instance keeps casting until it restarts.

  The application is correct either way: with the columns still ``json`` it
  casts per query, which is slower but not broken. Converting is an
  optimisation, not a repair. If a maintenance window is hard to find, it is
  entirely reasonable to leave this until one appears.

USAGE

    DATABASE_URL=postgresql://... python -m scripts.convert_json_to_jsonb --dry-run
    DATABASE_URL=postgresql://... python -m scripts.convert_json_to_jsonb --confirm-outage

  --dry-run reports the current types, row count and table size, and exits
  without touching anything. Converting requires --confirm-outage.
"""

import argparse
import os
import sys
import time

from sqlalchemy import create_engine, text

#: Converted one at a time, each in its own transaction, so a timeout on a
#: later column keeps the earlier ones. The Alembic version wrapped all four in
#: one transaction, so the first timeout rolled back everything.
JSON_COLUMNS = ("ai_analysis", "keywords", "file_metadata", "embedding_provenance")

#: How long to wait for the exclusive lock before giving up on one attempt.
#: Deliberately brief: while this request is pending it also blocks every new
#: reader queuing behind it, so a long wait is itself an outage.
LOCK_TIMEOUT = "3s"

#: Attempts per column, with a pause between. With the services scaled down
#: the lock should be free on the first try; the retries exist for a stray
#: connection, not for running against live traffic.
ATTEMPTS = 5
PAUSE_SECONDS = 5


def column_types(conn):
    rows = conn.execute(
        text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'documents' "
            "AND column_name = ANY(:cols)"
        ),
        {"cols": list(JSON_COLUMNS)},
    ).fetchall()
    return {name: kind for name, kind in rows}


def report(engine):
    with engine.connect() as conn:
        print("=" * 70)
        print("CURRENT STATE")
        print("=" * 70)
        rows = conn.execute(text("SELECT COUNT(*) FROM documents")).scalar()
        size = conn.execute(
            text("SELECT pg_size_pretty(pg_total_relation_size('documents'))")
        ).scalar()
        print(f"  documents: {rows:,} rows, {size} on disk")
        print()
        types = column_types(conn)
        pending = []
        for column in JSON_COLUMNS:
            kind = types.get(column)
            if kind is None:
                print(f"  {column:24} (absent)")
            elif kind == "jsonb":
                print(f"  {column:24} jsonb   already converted")
            else:
                print(f"  {column:24} {kind}    NEEDS CONVERSION")
                pending.append(column)
        print()
        idx = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'documents' AND indexname = 'idx_documents_keywords'"
            )
        ).scalar()
        print(f"  keywords GIN index: {idx or 'absent'}")
        return pending


def convert(engine, column) -> bool:
    """
    Convert one column, retrying until a lock gap appears.

    Each attempt is its own transaction with a short ``lock_timeout``. Holding
    a long lock request would queue every other session behind us — far worse
    than failing this attempt and trying again shortly.
    """
    for attempt in range(1, ATTEMPTS + 1):
        started = time.perf_counter()
        try:
            with engine.begin() as conn:
                conn.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
                conn.execute(
                    text(
                        f"ALTER TABLE documents "
                        f"ALTER COLUMN {column} TYPE jsonb USING {column}::jsonb"
                    )
                )
            elapsed = time.perf_counter() - started
            print(f"  {column}: converted in {elapsed:.1f}s")
            return True
        except Exception as e:
            if "lock" not in str(e).lower():
                print(f"  {column}: FAILED — {e}")
                return False
            print(
                f"  {column}: attempt {attempt}/{ATTEMPTS} could not take the "
                f"lock, retrying in {PAUSE_SECONDS}s"
            )
            time.sleep(PAUSE_SECONDS)
    print(f"  {column}: gave up after {ATTEMPTS} attempts — retry when quieter")
    return False


def rebuild_index(engine):
    """
    Recreate the GIN index on ``keywords``, CONCURRENTLY so it does not lock.

    ``jsonb_path_ops`` is about half the size of the default opclass and covers
    the containment queries this column is actually searched by. CONCURRENTLY
    cannot run inside a transaction, hence the autocommit connection.
    """
    print()
    print("Rebuilding the keywords GIN index (concurrently, no lock)...")
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        try:
            conn.execute(text("DROP INDEX IF EXISTS idx_documents_keywords"))
            conn.execute(
                text(
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_documents_keywords "
                    "ON documents USING gin (keywords jsonb_path_ops)"
                )
            )
            print("  index rebuilt")
        except Exception as e:
            print(f"  index rebuild FAILED — {e}")
            print("  the conversion itself still stands; retry the index later")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="report current types and exit without changing anything",
    )
    ap.add_argument(
        "--confirm-outage",
        action="store_true",
        help=(
            "required to convert: acknowledges that documents is locked for "
            "the whole rewrite and the application will be unavailable"
        ),
    )
    ap.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    args = ap.parse_args()

    if not args.database_url:
        sys.exit("DATABASE_URL is not set.")
    url = args.database_url
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    engine = create_engine(url, pool_pre_ping=True)
    pending = report(engine)

    if not pending:
        print()
        print("Nothing to convert.")
        return 0

    if args.dry_run:
        print()
        print(f"Dry run: {len(pending)} column(s) would be converted.")
        return 0

    if not args.confirm_outage:
        print()
        print("=" * 70)
        print("REFUSING TO RUN WITHOUT --confirm-outage")
        print("=" * 70)
        print("  ALTER TABLE holds ACCESS EXCLUSIVE on documents for the whole")
        print("  rewrite. Every query blocks for the duration, and the app")
        print("  becomes unavailable — this is not a background operation.")
        print()
        print("  Scale the web service and celery-worker to 0 and suspend the")
        print("  dropbox-ingest cron, then re-run with --confirm-outage.")
        print()
        print("  The app works correctly without this conversion; it just")
        print("  casts per query. Postponing is a valid choice.")
        return 2

    print()
    print("=" * 70)
    print("CONVERTING")
    print("=" * 70)
    print("Locking documents now. Anything still querying it will block.")
    print()

    converted = [c for c in pending if convert(engine, c)]

    if "keywords" in converted:
        rebuild_index(engine)

    print()
    print("=" * 70)
    print(f"{len(converted)} of {len(pending)} converted")
    if len(converted) < len(pending):
        print("Re-run to finish the rest — converted columns are skipped.")
        return 1
    print()
    print("Restart the web service so it re-detects the column types and")
    print("stops casting (services/metrics/jsonb.py caches the detection per")
    print("process). Then restore celery-worker and dropbox-ingest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
