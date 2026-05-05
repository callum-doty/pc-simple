"""
Client Normalization Pipeline — Step 1: Deterministic Cleaning
Produces client_clean_v1 from client_raw.

Stages:
  1. Mechanical noise removal (newlines, emoji, punctuation, casing, "the" prefix)
  2. Garbage detection (too short, known bad patterns) → NULL
  3. Outputs:
       - normalization_preview.csv  (id, client_raw, client_clean_v1, flagged_garbage)
       - canonical_map_template.csv (client_clean_v1, count, client_canonical ← blank for manual fill)
  4. --push flag: writes client_clean_v1 column to Render DB

Usage:
    python normalize_clients.py --csv all_predictions.csv --output-dir .
    python normalize_clients.py --csv all_predictions.csv --output-dir . \\
        --push --db-url "postgresql://..."
"""

import re
import csv
import argparse
from collections import defaultdict

# ---------------------------------------------------------------------------
# GARBAGE PATTERNS
# Words/phrases that indicate a failed extraction, not a real client name.
# These will be set to NULL in client_clean_v1.
# ---------------------------------------------------------------------------

GARBAGE_PATTERNS = [
    # Leftover disclaimer fragments
    re.compile(r'^not\s+authorized\b', re.IGNORECASE),
    re.compile(r'\bnot\s+authorized\s+by\s+any\s+candidate\b', re.IGNORECASE),
    re.compile(r'^official\s+funds\s+(authorized|of)\b', re.IGNORECASE),
    re.compile(r"^the\s+u\.?s\.?\s+chamber\s+of\s+commerce\s+and\s+not", re.IGNORECASE),

    # Leftover body copy fragments
    re.compile(r"^challenger\s+jay\s+chen", re.IGNORECASE),
    re.compile(r"^but\s+disappears", re.IGNORECASE),
    re.compile(r"^socialists\s+warnock", re.IGNORECASE),
    re.compile(r"^vote\s*\n*vote", re.IGNORECASE),
    re.compile(r"^no\s+on\s+allred", re.IGNORECASE),
    re.compile(r"^no\s*\n+on\b", re.IGNORECASE),
    re.compile(r"^(yes|no)\s+(on|off)\s*$", re.IGNORECASE),
    re.compile(r"^meeks\s*\n+for\s+judge", re.IGNORECASE),
    re.compile(r"^licensed\s+registered\s+trademark", re.IGNORECASE),
    re.compile(r"^regular\s+unleaded", re.IGNORECASE),
    re.compile(r"^late\s+past\s+due", re.IGNORECASE),
    re.compile(r"^new\s+location", re.IGNORECASE),
    re.compile(r"^by\s+mail\b", re.IGNORECASE),
    re.compile(r"^candidates\s+this\s+november", re.IGNORECASE),
    re.compile(r"^place\s+first\s+class", re.IGNORECASE),
    re.compile(r"^ccs\s+scs\s+hcs", re.IGNORECASE),
    re.compile(r"^sided\s+with\s+them", re.IGNORECASE),
    re.compile(r"^lying\s+about", re.IGNORECASE),
    re.compile(r"^too\s+much\b", re.IGNORECASE),
    re.compile(r"^office\s+(locations\s+)?washington", re.IGNORECASE),
    re.compile(r"^him\s+here\s+kulkarni", re.IGNORECASE),
    re.compile(r"^unpaid\s+(late|overdue)", re.IGNORECASE),
    re.compile(r"^past\s+due\s+late", re.IGNORECASE),
    re.compile(r"^constitutional\s+term\s+limits", re.IGNORECASE),
    re.compile(r"^insight\s+(what|will|ted|jay|sugar|wendy|waylon|martha|anthony)\b", re.IGNORECASE),
    re.compile(r"^(behind|against|insight)\s+\w+", re.IGNORECASE),
]

# Single/very short words that are always garbage
GARBAGE_SINGLE_WORDS = {
    "THE", "AND", "OR", "A", "AN", "FOR", "OF", "IN", "ON", "AT", "TO",
    "COMMITTEE", "COMMITTEE TO", "COMMITTEE TO ELECT", "THE COMMITTEE TO ELECT",
    "REPUBLICAN", "DEMOCRAT", "DEMOCRATIC", "PARTY",
}

# Minimum token count after cleaning — anything shorter is garbage
MIN_TOKEN_COUNT = 2


# ---------------------------------------------------------------------------
# NORMALIZATION
# ---------------------------------------------------------------------------

def normalize_client(raw):
    """
    Deterministic normalization pipeline.
    Returns cleaned string or None if garbage/empty.
    """
    if not raw:
        return None

    s = raw.strip()

    # 1. Collapse all whitespace (including newlines, tabs)
    s = re.sub(r"\s+", " ", s)

    # 2. Remove emoji and non-ASCII symbols (keep letters, digits, &.,'-()#@/)
    s = re.sub(r"[^\w\s&.,'\-()\/#@]", "", s)

    # 3. Normalize remaining whitespace after symbol removal
    s = re.sub(r"\s+", " ", s).strip()

    # 4. Remove trailing punctuation/noise
    s = re.sub(r"[,\.\-:\s]+$", "", s).strip()

    # 5. Remove leading "the " (case-insensitive)
    s = re.sub(r"^the\s+", "", s, flags=re.IGNORECASE).strip()

    # 6. Title-case
    s = s.title()

    # Garbage checks
    if not s:
        return None

    if s.upper() in GARBAGE_SINGLE_WORDS:
        return None

    # Too few tokens
    if len(s.split()) < MIN_TOKEN_COUNT:
        return None

    # Pattern-based garbage detection (run on original raw for broader matching)
    for pattern in GARBAGE_PATTERNS:
        if pattern.search(raw):
            return None

    return s


def is_garbage(raw, cleaned):
    """Returns True if the raw value is a known-bad extraction."""
    if cleaned is None:
        return True
    return False


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run(csv_path, output_dir, db_url=None, push=False):
    # Load predictions
    rows = []
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    print(f"Loaded {len(rows)} rows from {csv_path}")

    # Apply normalization
    freq = defaultdict(int)       # client_clean_v1 → count
    garbage_count = 0
    unchanged_count = 0
    changed_count = 0

    for row in rows:
        raw = (row.get("client") or "").strip()
        cleaned = normalize_client(raw)
        row["client_clean_v1"] = cleaned or ""
        row["flagged_garbage"] = "TRUE" if is_garbage(raw, cleaned) else "FALSE"

        if cleaned:
            freq[cleaned] += 1
            if cleaned.lower() == raw.lower():
                unchanged_count += 1
            else:
                changed_count += 1
        else:
            garbage_count += 1

    print(f"\n  Changed:   {changed_count}")
    print(f"  Unchanged: {unchanged_count}")
    print(f"  → NULL (garbage/empty): {garbage_count}")
    print(f"  Unique client_clean_v1 values: {len(freq)}")

    # --- Output 1: Full preview CSV ---
    preview_path = f"{output_dir}/normalization_preview.csv"
    with open(preview_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=["id", "filename", "client_raw", "client_clean_v1", "flagged_garbage"])
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "id": row.get("id", ""),
                "filename": row.get("filename", ""),
                "client_raw": row.get("client", ""),
                "client_clean_v1": row.get("client_clean_v1", ""),
                "flagged_garbage": row.get("flagged_garbage", ""),
            })
    print(f"\nNormalization preview written to: {preview_path}")

    # --- Output 2: Canonical map template ---
    # Sorted by frequency descending — top entries are highest leverage for manual mapping
    map_path = f"{output_dir}/canonical_map_template.csv"
    with open(map_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(["client_clean_v1", "count", "client_canonical"])
        for name, count in sorted(freq.items(), key=lambda x: -x[1]):
            writer.writerow([name, count, ""])  # client_canonical blank for manual fill
    print(f"Canonical map template written to: {map_path}")
    print(f"  {len(freq)} unique names — fill in client_canonical for top entries first")
    print(f"  Top 10 by frequency:")
    for name, count in sorted(freq.items(), key=lambda x: -x[1])[:10]:
        print(f"    [{count:4d}]  {name}")

    # --- Optional: Push to DB ---
    if push:
        if not db_url:
            print("\nERROR: --push requires --db-url")
            return
        _push_to_db(rows, db_url)


def _push_to_db(rows, db_url):
    import psycopg2
    from psycopg2.extras import execute_values

    print(f"\nConnecting to database...")
    conn = psycopg2.connect(db_url)
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            # Add column if not exists
            print("Adding client_clean_v1 column (if not exists)...")
            cur.execute("""
                ALTER TABLE documents
                    ADD COLUMN IF NOT EXISTS client_clean_v1 TEXT;
            """)

            # Build update payload
            data = [
                (row.get("client_clean_v1") or None, int(row["id"]))
                for row in rows
                if row.get("id")
            ]

            print(f"Updating {len(data)} rows...")
            execute_values(
                cur,
                """
                UPDATE documents d
                SET client_clean_v1 = v.val,
                    updated_at = NOW()
                FROM (VALUES %s) AS v(val, id)
                WHERE d.id = v.id
                """,
                data,
                template="(%s::text, %s::integer)",
                page_size=200
            )
            updated = cur.rowcount
            conn.commit()
            print(f"Done. {updated} rows updated with client_clean_v1.")

    except Exception as e:
        conn.rollback()
        print(f"\nError — transaction rolled back: {e}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deterministic client name normalization")
    parser.add_argument("--csv", required=True, help="Path to all_predictions.csv")
    parser.add_argument("--output-dir", default=".", help="Directory for output files")
    parser.add_argument("--push", action="store_true", help="Push client_clean_v1 to Render DB")
    parser.add_argument("--db-url", help="Render external DB connection string (required with --push)")
    args = parser.parse_args()

    run(args.csv, args.output_dir, db_url=args.db_url, push=args.push)
