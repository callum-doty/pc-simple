"""
Seed canonical_overrides table from canonical_map_template.csv.

Only imports rows where client_canonical is filled in (non-empty).
Safe to re-run — uses INSERT ... ON CONFLICT DO UPDATE so existing rows
are updated rather than duplicated.

Usage:
    python feature_extraction/seed_canonical_overrides.py \\
        --csv feature_extraction/canonical_map_template.csv \\
        --db-url "postgresql://..."

    # Dry run (no DB writes):
    python feature_extraction/seed_canonical_overrides.py \\
        --csv feature_extraction/canonical_map_template.csv \\
        --db-url "postgresql://..." --dry-run

After running, you can discard canonical_map_template.csv — the DB is now
the single source of truth. Future overrides go directly into the
canonical_overrides table (or via whatever admin UI you build).

To find documents affected by a changed override and re-run extraction:

    SELECT id FROM documents WHERE client_clean_v1 = '<raw name>';

Then call:
    extract_document_features_task.delay(document_id, force=True)
"""

import argparse
import csv
import sys

import psycopg2
from psycopg2.extras import execute_values


UPSERT_SQL = """
INSERT INTO canonical_overrides (client_clean_v1, client_canonical)
VALUES %s
ON CONFLICT (client_clean_v1)
DO UPDATE SET
    client_canonical = EXCLUDED.client_canonical,
    updated_at       = NOW()
"""


def load_overrides(csv_path: str) -> list[tuple]:
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            clean = (row.get("client_clean_v1") or "").strip()
            canonical = (row.get("client_canonical") or "").strip()
            if clean and canonical:
                rows.append((clean, canonical))
    return rows


def run(csv_path: str, db_url: str, dry_run: bool = False) -> None:
    rows = load_overrides(csv_path)
    print(f"Found {len(rows)} filled overrides in {csv_path}.")

    if not rows:
        print("Nothing to import.")
        return

    if dry_run:
        print("\n[DRY RUN] No changes written. First 10 rows:")
        for clean, canonical in rows[:10]:
            print(f"  {clean!r:60s} → {canonical!r}")
        return

    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            execute_values(cur, UPSERT_SQL, rows, page_size=200)
            conn.commit()
            print(f"Upserted {len(rows)} overrides into canonical_overrides.")
    except Exception as exc:
        conn.rollback()
        print(f"Error — rolled back: {exc}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed canonical_overrides from CSV")
    parser.add_argument("--csv", required=True, help="Path to canonical_map_template.csv")
    parser.add_argument("--db-url", required=True, help="PostgreSQL connection string")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()
    run(args.csv, args.db_url, dry_run=args.dry_run)
