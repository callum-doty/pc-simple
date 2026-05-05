"""
DB Update Script
Reads a production predictions CSV and updates the documents table on Render.

Usage:
    python db_update.py --csv all_predictions.csv --db-url "postgresql://..."

Steps performed:
    1. Adds new columns to documents table if they don't exist
    2. Loads predictions CSV into a temp table
    3. Batch-updates documents from temp table
    4. Drops temp table
    5. Prints summary (rows updated, rows flagged for review)
"""

import csv
import sys
import argparse
import psycopg2
from psycopg2.extras import execute_values

# ---------------------------------------------------------------------------
# SCHEMA MIGRATION
# ---------------------------------------------------------------------------

ADD_COLUMNS_SQL = """
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS date_created       DATE,
    ADD COLUMN IF NOT EXISTS client             TEXT,
    ADD COLUMN IF NOT EXISTS paid_for_by_raw    TEXT,
    ADD COLUMN IF NOT EXISTS state              CHAR(2),
    ADD COLUMN IF NOT EXISTS is_frank           BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS date_confidence    TEXT,
    ADD COLUMN IF NOT EXISTS client_confidence  TEXT,
    ADD COLUMN IF NOT EXISTS state_confidence   TEXT,
    ADD COLUMN IF NOT EXISTS needs_review       BOOLEAN DEFAULT FALSE;
"""

# ---------------------------------------------------------------------------
# TEMP TABLE
# ---------------------------------------------------------------------------

CREATE_TEMP_SQL = """
CREATE TEMP TABLE _extraction_staging (
    doc_id              INTEGER,
    date_created        DATE,
    date_confidence     TEXT,
    client              TEXT,
    client_confidence   TEXT,
    paid_for_by_raw     TEXT,
    is_frank            BOOLEAN,
    state               CHAR(2),
    state_confidence    TEXT,
    needs_review        BOOLEAN
);
"""

UPDATE_SQL = """
UPDATE documents d
SET
    date_created        = s.date_created,
    date_confidence     = s.date_confidence,
    client              = s.client,
    client_confidence   = s.client_confidence,
    paid_for_by_raw     = s.paid_for_by_raw,
    is_frank            = s.is_frank,
    state               = s.state,
    state_confidence    = s.state_confidence,
    needs_review        = s.needs_review,
    updated_at          = NOW()
FROM _extraction_staging s
WHERE d.id = s.doc_id;
"""

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def parse_bool(val):
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("true", "1", "yes")


def parse_nullable(val):
    if val is None or str(val).strip() in ("", "None", "NULL"):
        return None
    return str(val).strip()


def parse_date(val):
    """Validate date string before sending to PostgreSQL. Returns None if invalid."""
    from dateutil import parser as dateutil_parser
    raw = parse_nullable(val)
    if raw is None:
        return None
    try:
        parsed = dateutil_parser.parse(raw)
        return parsed.strftime("%Y-%m-%d")
    except Exception:
        return None


def load_csv(path):
    rows = []
    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append((
                int(row["id"]),
                parse_date(row.get("date_created")),
                parse_nullable(row.get("date_confidence")),
                parse_nullable(row.get("client")),
                parse_nullable(row.get("client_confidence")),
                parse_nullable(row.get("paid_for_by_raw")),
                parse_bool(row.get("is_frank", False)),
                parse_nullable(row.get("state")),
                parse_nullable(row.get("state_confidence")),
                parse_bool(row.get("needs_review", False)),
            ))
    return rows


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run(csv_path, db_url, dry_run=False):
    print(f"Loading predictions from: {csv_path}")
    rows = load_csv(csv_path)
    print(f"  {len(rows)} documents loaded.")

    flagged = sum(1 for r in rows if r[9])  # needs_review is index 9
    print(f"  {flagged} flagged for review ({len(rows) - flagged} fully confident).")

    if dry_run:
        print("\n[DRY RUN] No changes written to database.")
        return

    print(f"\nConnecting to database...")
    conn = psycopg2.connect(db_url)
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            # Step 1: Add columns
            print("Step 1/4 — Adding new columns (if not exist)...")
            cur.execute(ADD_COLUMNS_SQL)

            # Step 2: Create temp table and load data
            print("Step 2/4 — Loading predictions into staging table...")
            cur.execute(CREATE_TEMP_SQL)
            execute_values(
                cur,
                """
                INSERT INTO _extraction_staging (
                    doc_id, date_created, date_confidence,
                    client, client_confidence, paid_for_by_raw,
                    is_frank, state, state_confidence, needs_review
                ) VALUES %s
                """,
                rows,
                page_size=200
            )

            # Step 3: Update documents from staging
            print("Step 3/4 — Updating documents table...")
            cur.execute(UPDATE_SQL)
            updated = cur.rowcount
            print(f"  {updated} rows updated.")

            # Step 4: Cleanup
            print("Step 4/4 — Cleaning up staging table...")
            cur.execute("DROP TABLE IF EXISTS _extraction_staging;")

            conn.commit()
            print(f"\nDone. {updated} documents updated in Render.")
            print(f"{flagged} rows marked needs_review=TRUE for manual follow-up.")

    except Exception as e:
        conn.rollback()
        print(f"\nError — transaction rolled back: {e}")
        sys.exit(1)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Push extraction predictions to Render PostgreSQL")
    parser.add_argument("--csv", required=True, help="Path to production predictions CSV")
    parser.add_argument("--db-url", required=True, help="Render external DB connection string")
    parser.add_argument("--dry-run", action="store_true",
                        help="Load and validate CSV without writing to DB")
    args = parser.parse_args()
    run(args.csv, args.db_url, dry_run=args.dry_run)
