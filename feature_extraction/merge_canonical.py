"""
Client Canonical Merge
Combines AI extraction + heuristic normalization into client_canonical.

Source priority (highest → lowest):
  1. AI extraction (ai_client_extract.csv) — semantic, HIGH/MEDIUM confidence
  2. client_clean_v1 (from all_predictions.csv) — deterministic normalization
  3. NULL — if both sources are empty

Output:
  canonical_merged.csv  — id, filename, client_canonical, canonical_source, ai_confidence
  + optional --push to write client_canonical to Render

Usage:
    python merge_canonical.py \
        --predictions all_predictions.csv \
        --ai-extract ai_client_extract.csv \
        --output canonical_merged.csv

    python merge_canonical.py \
        --predictions all_predictions.csv \
        --ai-extract ai_client_extract.csv \
        --output canonical_merged.csv \
        --push \
        --db-url "postgresql://..."
"""

import re
import csv
import argparse
from collections import Counter

# ---------------------------------------------------------------------------
# NORMALIZATION (same rules as normalize_clients.py — applied to AI output)
# ---------------------------------------------------------------------------

TITLE_PREFIX_RE = re.compile(
    r'^(?:Congressman|Congresswoman|Senator|Representative|Rep\.'
    r'|Assemblywoman|Assemblyman|Sheriff|Mayor|Judge|Dr\.'
    r'|Governor|Lt\.\s+Governor|Attorney\s+General'
    r'|Councilman|Councilwoman|Commissioner|Supervisor|Trustee)\s+',
    re.IGNORECASE
)

OFFICE_SUFFIX_RE = re.compile(
    r'\s+(?:for\s+)?(?:u\.?s\.?\s+)?(?:'
    r'congress(?:man|woman)?|senate|senator|assembly(?:man|woman)?'
    r'|governor|attorney\s+general|mayor|sheriff|judge'
    r'|state\s+\w+|county\s+\w+|city\s+\w+|school\s+board'
    r'|district\s+\d+|house|representative'
    r')\s*(?:\d{4})?\s*$',
    re.IGNORECASE
)

STATE_NAME_TO_ABBR = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY", "District Of Columbia": "DC",
}

ALL_STATES = sorted(STATE_NAME_TO_ABBR.keys(), key=len, reverse=True)
STATE_PATTERN = '|'.join(re.escape(s) for s in ALL_STATES)

PARTY_NORMALIZE_RE = re.compile(
    rf'^(?:Republican\s+Party\s+Of\s+|the\s+Republican\s+Party\s+Of\s+)?({STATE_PATTERN})\s+Republican\s+Party$'
    rf'|^Republican\s+Party\s+Of\s+({STATE_PATTERN})$'
    rf'|^({STATE_PATTERN})n?\s+Republican\s+Party$'
    rf'|^({STATE_PATTERN})\s+G\.?O\.?P\.?$'
    rf'|^(?:The\s+)?({STATE_PATTERN})\s+Republican$',
    re.IGNORECASE
)


def normalize_party(name):
    """Normalize all Republican party variants to '[State] Republican Party'."""
    m = PARTY_NORMALIZE_RE.match(name)
    if m:
        state = next((g for g in m.groups() if g), None)
        if state:
            # Strip trailing 'n' for "Nebraskan" → "Nebraska"
            state = re.sub(r'n$', '', state.strip(), flags=re.IGNORECASE)
            state = state.title()
            if state in STATE_NAME_TO_ABBR or state.rstrip('n') in STATE_NAME_TO_ABBR:
                return f"{state} Republican Party"
    return None


# Fixes for .title() breaking compound caps: Mc, Mac, O', De, La, etc.
COMPOUND_CAP_RE = re.compile(r"\b(Mc|Mac|O')([a-z])")


def fix_compound_caps(name):
    """Restore Mc/Mac/O' capitalization broken by .title()"""
    return COMPOUND_CAP_RE.sub(lambda m: m.group(1) + m.group(2).upper(), name)


def clean_ai_name(name):
    """Apply normalization rules to AI-extracted names."""
    if not name:
        return None
    name = re.sub(r'\s+', ' ', name).strip()
    # Strip leading "the " / "The "
    name = re.sub(r'^the\s+', '', name, flags=re.IGNORECASE).strip()
    name = TITLE_PREFIX_RE.sub('', name).strip()
    name = OFFICE_SUFFIX_RE.sub('', name).strip()
    name = re.sub(r'[,\.\-:]+$', '', name).strip()
    name = name.title()
    name = fix_compound_caps(name)

    # Apply party normalization
    normalized = normalize_party(name)
    if normalized:
        return normalized

    # Must be at least 3 chars and not a single generic word
    if len(name) < 3 or name.lower() in {'the', 'and', 'for', 'of', 'in'}:
        return None

    return name


def clean_v1_normalize(name):
    """Apply party normalization + compound cap fix to client_clean_v1 fallbacks."""
    if not name:
        return None
    name = re.sub(r'^the\s+', '', name, flags=re.IGNORECASE).strip()
    normalized = normalize_party(name)
    if normalized:
        return normalized
    return fix_compound_caps(name)


# ---------------------------------------------------------------------------
# MERGE LOGIC
# ---------------------------------------------------------------------------

def load_ai_extract(path):
    """Returns dict: id → {client_from_ai, ai_confidence, source_tier}"""
    result = {}
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            result[row['id']] = {
                'client_from_ai': (row.get('client_from_ai') or '').strip(),
                'ai_confidence': (row.get('ai_confidence') or '').strip(),
                'source_tier': (row.get('source_tier') or '').strip(),
            }
    return result


def load_predictions(path):
    """Returns list of prediction rows."""
    rows = []
    with open(path, newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    return rows


def merge(predictions, ai_extract):
    """
    Merges AI extraction + client_clean_v1 into client_canonical.
    Returns list of result dicts.
    """
    results = []
    source_counts = Counter()

    for row in predictions:
        doc_id = row.get('id', '')
        ai_data = ai_extract.get(doc_id, {})

        ai_name = clean_ai_name(ai_data.get('client_from_ai') or '')
        ai_conf = ai_data.get('ai_confidence', '')
        ai_tier = ai_data.get('source_tier', '')
        clean_v1_raw = (row.get('client_clean_v1') or row.get('client') or '').strip()
        clean_v1 = clean_v1_normalize(clean_v1_raw) or clean_v1_raw

        # Priority 1: AI extraction with HIGH confidence
        if ai_name and ai_conf == 'HIGH':
            canonical = ai_name
            source = f'ai_{ai_tier}'

        # Priority 2: AI extraction with MEDIUM confidence (only if no clean_v1)
        elif ai_name and ai_conf == 'MEDIUM' and not clean_v1:
            canonical = ai_name
            source = f'ai_{ai_tier}_medium'

        # Priority 3: AI MEDIUM + clean_v1 both exist → prefer clean_v1
        # (heuristic paid_for_by is more reliable for org identity than summary)
        elif clean_v1 and ai_name and ai_conf == 'MEDIUM':
            canonical = clean_v1
            source = 'clean_v1_over_ai_medium'

        # Priority 4: clean_v1 fallback
        elif clean_v1:
            canonical = clean_v1
            source = 'clean_v1_fallback'

        # Priority 5: AI MEDIUM as last resort
        elif ai_name:
            canonical = ai_name
            source = f'ai_{ai_tier}_last_resort'

        else:
            canonical = None
            source = 'null'

        source_counts[source] += 1

        results.append({
            'id': doc_id,
            'filename': row.get('filename', ''),
            'client_raw': row.get('client', ''),
            'client_clean_v1': clean_v1,
            'client_from_ai': ai_data.get('client_from_ai', ''),
            'ai_confidence': ai_conf,
            'client_canonical': canonical or '',
            'canonical_source': source,
        })

    return results, source_counts


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run(predictions_path, ai_extract_path, output_path, db_url=None, push=False):
    predictions = load_predictions(predictions_path)
    ai_extract = load_ai_extract(ai_extract_path)

    print(f"Loaded {len(predictions)} predictions, {len(ai_extract)} AI extractions")

    results, source_counts = merge(predictions, ai_extract)

    # Stats
    extracted = sum(1 for r in results if r['client_canonical'])
    total = len(results)
    print(f"\nMerge results: {extracted}/{total} ({extracted/total:.0%}) have client_canonical")
    print(f"\nBy source:")
    for source, count in sorted(source_counts.items(), key=lambda x: -x[1]):
        print(f"  {source:<40} {count:>5}")

    # Unique canonical names
    canonical_freq = Counter(r['client_canonical'] for r in results if r['client_canonical'])
    print(f"\nUnique canonical names: {len(canonical_freq)}")
    print(f"\nTop 15 by frequency:")
    for name, count in canonical_freq.most_common(15):
        print(f"  [{count:4d}]  {name}")

    # Write output CSV
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'id', 'filename', 'client_raw', 'client_clean_v1',
            'client_from_ai', 'ai_confidence', 'client_canonical', 'canonical_source'
        ])
        writer.writeheader()
        writer.writerows(results)
    print(f"\nMerged output written to: {output_path}")

    if push:
        if not db_url:
            print("ERROR: --push requires --db-url")
            return
        _push_to_db(results, db_url)


def _push_to_db(results, db_url):
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
                (r['client_canonical'] or None, int(r['id']))
                for r in results
                if r.get('id')
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
                page_size=500
            )
            conn.commit()
            print(f"Done. client_canonical written to Render.")

            # Verify
            cur.execute("SELECT COUNT(*) FROM documents WHERE client_canonical IS NOT NULL")
            count = cur.fetchone()[0]
            print(f"Verified: {count} rows now have client_canonical set.")

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
    parser.add_argument("--predictions", required=True, help="Path to all_predictions.csv")
    parser.add_argument("--ai-extract", required=True, help="Path to ai_client_extract.csv")
    parser.add_argument("--output", default="canonical_merged.csv")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--db-url")
    args = parser.parse_args()

    run(args.predictions, args.ai_extract, args.output, db_url=args.db_url, push=args.push)
