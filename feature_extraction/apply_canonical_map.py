"""
Client Canonical Map — Step 2: Entity Resolution
Applies rule-based transformations + manual overrides to produce client_canonical.

Rule precedence (highest → lowest):
  1. Manual override from canonical_map_template.csv (if client_canonical filled in)
  2. Programmatic rules (party normalization, office stripping, committee cleanup)
  3. Fall through to client_clean_v1 as-is

Usage:
    # Preview — no DB changes
    python apply_canonical_map.py --csv all_predictions.csv --map canonical_map_template.csv

    # Push to Render
    python apply_canonical_map.py --csv all_predictions.csv --map canonical_map_template.csv \\
        --push --db-url "postgresql://..."
"""

import re
import csv
import argparse
from collections import defaultdict

# ---------------------------------------------------------------------------
# STATE LOOKUP (for party normalization)
# ---------------------------------------------------------------------------

STATE_ABBR_TO_NAME = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District Of Columbia",
}

STATE_NAME_TO_ABBR = {v: k for k, v in STATE_ABBR_TO_NAME.items()}

# All state names sorted longest-first for safe regex alternation
ALL_STATE_NAMES = sorted(STATE_ABBR_TO_NAME.values(), key=len, reverse=True)
STATE_NAME_PATTERN = '|'.join(re.escape(s) for s in ALL_STATE_NAMES)

# Office titles to strip from candidate names
OFFICE_STRIP_PATTERN = re.compile(
    r'\s+(?:for\s+)?(?:'
    r'u\.?s\.?\s+congress|congress(?:man|woman)?'
    r'|u\.?s\.?\s+senate|senate|senator'
    r'|u\.?s\.?\s+house|house(?:\s+of\s+representatives?)?'
    r'|state\s+(?:senate|house|assembly|representative|rep)'
    r'|assembly(?:man|woman)?'
    r'|governor|lt\.?\s+governor|lieutenant\s+governor'
    r'|attorney\s+general'
    r'|comptroller|treasurer|secretary\s+of\s+state'
    r'|(?:county\s+)?(?:district\s+)?(?:superior\s+)?(?:appellate\s+)?judge'
    r'|sheriff|mayor|city\s+council(?:man|woman|member)?'
    r'|school\s+board(?:\s+member)?'
    r'|supervisor|commissioner|clerk'
    r'|district\s+\d+'
    r'|[a-z]+\s+\d+(?:th|st|nd|rd)?'
    r')\s*(?:\d{4})?\s*$',
    re.IGNORECASE
)

# Committee suffixes to strip from committee names (treasurer names, addresses)
COMMITTEE_TREASURER_STRIP = re.compile(
    r'\s*[-–—,]\s*(?:'
    r'[A-Z][a-z]+\s+[A-Z][a-z]+'           # Person name: "Ron Richard"
    r'|[A-Z]{2,}\s+[A-Z]{2,}'              # ALL CAPS person: "RON RICHARD"
    r'|treasurer|chairman|chair|registered\s+agent'
    r')\s*$',
    re.IGNORECASE
)


# ---------------------------------------------------------------------------
# PROGRAMMATIC RULES
# ---------------------------------------------------------------------------

def apply_party_normalization(name):
    """
    Normalizes party/GOP names to "[State] Republican Party" format.
    Examples:
      Republican Party Of Texas        → Texas Republican Party
      Texas Gop                        → Texas Republican Party
      The Nebraska Republican          → Nebraska Republican Party
      Nebraskan Republican Party       → Nebraska Republican Party
    """
    # Pattern: "Republican Party Of [State]" or "[State] Republican Party"
    m = re.match(
        rf'^(?:Republican\s+Party\s+Of\s+|the\s+Republican\s+Party\s+Of\s+)?({STATE_NAME_PATTERN})\s+Republican\s+Party$',
        name, re.IGNORECASE
    )
    if m:
        state = m.group(1).title()
        return f"{state} Republican Party"

    m = re.match(
        rf'^Republican\s+Party\s+Of\s+({STATE_NAME_PATTERN})$',
        name, re.IGNORECASE
    )
    if m:
        state = m.group(1).title()
        return f"{state} Republican Party"

    # "Nebraskan Republican Party" → "Nebraska Republican Party"
    m = re.match(
        rf'^({STATE_NAME_PATTERN})n\s+Republican\s+Party$',
        name, re.IGNORECASE
    )
    if m:
        state = m.group(1).title()
        return f"{state} Republican Party"

    # "[State] GOP"
    m = re.match(rf'^({STATE_NAME_PATTERN})\s+G\.?O\.?P\.?$', name, re.IGNORECASE)
    if m:
        state = m.group(1).title()
        return f"{state} Republican Party"

    # Standalone "[State] Republican" (truncated)
    m = re.match(rf'^(?:The\s+)?({STATE_NAME_PATTERN})\s+Republican$', name, re.IGNORECASE)
    if m:
        state = m.group(1).title()
        return f"{state} Republican Party"

    # "Republican Party Of [State Abbr]" (e.g. "Republican Party Of Ia")
    m = re.match(r'^Republican\s+Party\s+Of\s+([A-Z]{2})$', name, re.IGNORECASE)
    if m:
        abbr = m.group(1).upper()
        if abbr in STATE_ABBR_TO_NAME:
            return f"{STATE_ABBR_TO_NAME[abbr].title()} Republican Party"

    return None


def apply_candidate_name_strip(name):
    """
    Strips trailing office/campaign suffixes from candidate names.
    Examples:
      Alex Mealer For Judge     → Alex Mealer
      Tony Buzbee For Mayor     → Tony Buzbee
      Bice For Congress         → Bice
      Cheryl Bean For Texas 97Th → Cheryl Bean
    """
    stripped = OFFICE_STRIP_PATTERN.sub('', name).strip()
    stripped = re.sub(r'\s+(?:Campaign|For\s+\w+)\s*$', '', stripped, flags=re.IGNORECASE).strip()

    # Only return if we actually removed something and result is still ≥ 1 word
    if stripped and stripped.lower() != name.lower() and len(stripped.split()) >= 1:
        return stripped
    return None


def apply_committee_cleanup(name):
    """
    Strips treasurer names and address artifacts from committee names.
    Example: "Missouri Senate Campaign Committee - Ron Richard" → "Missouri Senate Campaign Committee"
    """
    stripped = COMMITTEE_TREASURER_STRIP.sub('', name).strip()
    if stripped and stripped.lower() != name.lower():
        return stripped
    return None


def apply_rules(name):
    """
    Applies all programmatic rules in order. Returns canonical name or None.
    """
    if not name:
        return None

    # Rule 1: Party normalization (highest specificity)
    result = apply_party_normalization(name)
    if result:
        return result

    # Rule 2: Office strip from candidate names
    result = apply_candidate_name_strip(name)
    if result:
        return result

    # Rule 3: Committee cleanup
    result = apply_committee_cleanup(name)
    if result:
        return result

    return None


# ---------------------------------------------------------------------------
# RESOLVE — combines rules + manual map
# ---------------------------------------------------------------------------

def load_manual_map(map_path):
    """
    Loads the filled canonical_map_template.csv.
    Returns dict: client_clean_v1 → client_canonical (only rows where canonical is filled).
    """
    manual = {}
    with open(map_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            clean = (row.get("client_clean_v1") or "").strip()
            canonical = (row.get("client_canonical") or "").strip()
            if clean and canonical:
                manual[clean.lower()] = canonical
    return manual


def resolve_canonical(client_clean_v1, manual_map):
    """
    Returns (client_canonical, source) where source is one of:
      "manual"       — from filled CSV
      "rule"         — programmatic transformation applied
      "passthrough"  — no transformation, used client_clean_v1 as-is
    """
    if not client_clean_v1:
        return None, None

    # 1. Manual override (highest priority)
    if client_clean_v1.lower() in manual_map:
        return manual_map[client_clean_v1.lower()], "manual"

    # 2. Programmatic rules
    result = apply_rules(client_clean_v1)
    if result:
        return result, "rule"

    # 3. Passthrough
    return client_clean_v1, "passthrough"


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run(csv_path, map_path, db_url=None, push=False):
    manual_map = load_manual_map(map_path)
    print(f"Loaded {len(manual_map)} manual overrides from {map_path}")

    rows = []
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    # Apply canonical resolution to each row
    source_counts = defaultdict(int)
    canonical_freq = defaultdict(int)

    for row in rows:
        clean = (row.get("client_clean_v1") or "").strip() or None

        # Re-apply normalization if client_clean_v1 not in CSV (use client as fallback)
        if not clean:
            clean = (row.get("client") or "").strip() or None

        canonical, source = resolve_canonical(clean, manual_map)
        row["client_canonical"] = canonical or ""
        row["resolution_source"] = source or ""

        source_counts[source or "null"] += 1
        if canonical:
            canonical_freq[canonical] += 1

    # Summary
    print(f"\nResolution summary:")
    print(f"  manual override:  {source_counts['manual']:>5}")
    print(f"  rule-based:       {source_counts['rule']:>5}")
    print(f"  passthrough:      {source_counts['passthrough']:>5}")
    print(f"  null (no client): {source_counts['null']:>5}")
    print(f"\n  Unique canonical names: {len(canonical_freq)}")
    print(f"  (vs {sum(1 for r in rows if r.get('client_clean_v1'))} unique clean names)")

    print(f"\n  Top 15 canonical clients by document count:")
    for name, count in sorted(canonical_freq.items(), key=lambda x: -x[1])[:15]:
        print(f"    [{count:4d}]  {name}")

    # Preview CSV
    preview_path = "canonical_preview.csv"
    with open(preview_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=[
            "id", "filename", "client_raw", "client_clean_v1", "client_canonical", "resolution_source"
        ])
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "id": row.get("id", ""),
                "filename": row.get("filename", ""),
                "client_raw": row.get("client", ""),
                "client_clean_v1": row.get("client_clean_v1", ""),
                "client_canonical": row.get("client_canonical", ""),
                "resolution_source": row.get("resolution_source", ""),
            })
    print(f"\nPreview written to: {preview_path}")

    if push:
        if not db_url:
            print("ERROR: --push requires --db-url")
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
            print("Adding client_canonical column (if not exists)...")
            cur.execute("""
                ALTER TABLE documents
                    ADD COLUMN IF NOT EXISTS client_canonical TEXT;
            """)

            data = [
                (row.get("client_canonical") or None, int(row["id"]))
                for row in rows
                if row.get("id")
            ]

            print(f"Updating {len(data)} rows...")
            execute_values(
                cur,
                """
                UPDATE documents d
                SET client_canonical = v.val,
                    updated_at = NOW()
                FROM (VALUES %s) AS v(val, id)
                WHERE d.id = v.id
                """,
                data,
                template="(%s::text, %s::integer)",
                page_size=200
            )
            conn.commit()
            print(f"Done. client_canonical written to Render.")

    except Exception as e:
        conn.rollback()
        print(f"\nError — transaction rolled back: {e}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, help="Path to all_predictions.csv")
    parser.add_argument("--map", required=True, help="Path to canonical_map_template.csv (partially or fully filled)")
    parser.add_argument("--push", action="store_true", help="Push client_canonical to Render DB")
    parser.add_argument("--db-url", help="Render external DB connection string")
    args = parser.parse_args()

    run(args.csv, args.map, db_url=args.db_url, push=args.push)
