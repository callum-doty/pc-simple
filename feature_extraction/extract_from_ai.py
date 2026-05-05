"""
AI Analysis Client Extractor
Extracts client_canonical from ai_analysis JSON (summary + keyword_mappings).

Extraction tiers:
  Tier 1: keyword_mappings where mapped_canonical_term = "Name (Exact)"
           → verbatim_term is the candidate name (already AI-identified)
  Tier 2: Summary text patterns
           → "support [Name] for [Office]", "promoting [Name]'s reelection", etc.

Output:
  ai_client_extract.csv  — id, filename, client_from_ai, ai_confidence, source_tier

Usage:
    python extract_from_ai.py --csv ai_analysis.csv --output ai_client_extract.csv
"""

import re
import csv
import json
import argparse
from collections import Counter

# ---------------------------------------------------------------------------
# SUMMARY PARSING PATTERNS
# Ordered most-specific → least-specific
# ---------------------------------------------------------------------------

SUMMARY_PATTERNS = [
    # "support John Duarte for Congress" / "supporting John Duarte for Mayor"
    (re.compile(
        r'support(?:ing)?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\s+for\s+\w',
        re.IGNORECASE
    ), "HIGH"),

    # "promotes/promoting [Name]'s reelection/campaign"
    (re.compile(
        r'promot(?:es|ing)\s+(?:Republican\s+|Democratic\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\'s?\s+'
        r'(?:re-?election|campaign|bid|run)',
        re.IGNORECASE
    ), "HIGH"),

    # "re-election of [Name]"
    (re.compile(
        r're-?election\s+of\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})',
        re.IGNORECASE
    ), "HIGH"),

    # "elect/electing [Name]"
    (re.compile(
        r'elect(?:ing)?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\s+(?:as\s+|for\s+|\w)',
        re.IGNORECASE
    ), "HIGH"),

    # "mailer for/promoting [Name]" (with or without trailing office)
    (re.compile(
        r'(?:campaign\s+)?mailer\s+(?:for|promoting)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})',
        re.IGNORECASE
    ), "HIGH"),

    # "urging voters to support [Name]"
    (re.compile(
        r'urging\s+voters\s+to\s+(?:support|elect|vote\s+for)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})',
        re.IGNORECASE
    ), "HIGH"),

    # "[Name] for [Office] in [location/year]" — broad catch
    (re.compile(
        r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\s+for\s+'
        r'(?:Congress|Senate|Governor|Mayor|Assembly|House|Sheriff|Judge|'
        r'County|District|State\s+\w+|City\s+\w+|School\s+Board)',
        re.IGNORECASE
    ), "MEDIUM"),

    # "promotes [Name] as [office]" / "promotes [Name], [office]"
    (re.compile(
        r'promot(?:es|ing)\s+(?:Republican\s+|Democratic\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})'
        r'(?:\s+as|\s+for|,)',
        re.IGNORECASE
    ), "MEDIUM"),

    # "candidate [Name]" / "[Name], candidate"
    (re.compile(
        r'candidate\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})',
        re.IGNORECASE
    ), "MEDIUM"),

    # "highlights/featuring [Name]'s" or "highlights [Name] as"
    (re.compile(
        r'(?:highlights?|featuring)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})(?:\'s?\s+|\s+as\s+)',
        re.IGNORECASE
    ), "MEDIUM"),

    # "vote for [Name]" / "voting for [Name]"
    (re.compile(
        r'vot(?:e|ing)\s+for\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})',
        re.IGNORECASE
    ), "MEDIUM"),
]

# Words that disqualify a summary match (too generic / body-copy fragments)
SUMMARY_REJECT_WORDS = {
    "democrat", "republican", "amendment", "proposition", "measure", "ballot",
    "they", "this", "that", "their", "these", "those"
}


# Title/role prefixes to strip from extracted names
TITLE_PREFIX_RE = re.compile(
    r'^(?:Congressman|Congresswoman|Senator|Representative|Rep\.|Assemblywoman|Assemblyman'
    r'|Sheriff|Mayor|Judge|Dr\.|Governor|Lt\.\s+Governor|Attorney\s+General'
    r'|Councilman|Councilwoman|Commissioner|Supervisor|Trustee)\s+',
    re.IGNORECASE
)

# Office suffixes to strip from extracted names
OFFICE_SUFFIX_RE = re.compile(
    r'\s+(?:for|of)\s+(?:Congress|Senate|Assembly|Governor|Mayor|Sheriff|Judge'
    r'|County|District|State|City|School\s+Board|Clackamas|Texas|California'
    r'|\w+\s+County|\w+\s+District).*$',
    re.IGNORECASE
)


# Only truncate at prepositions that appear mid-name as artefacts of pattern over-capture
# e.g. "Luis Terrazas For New Mexico" → truncate at "For" → "Luis Terrazas"
# Deliberately narrow — don't include "of" (used in real names: "Bill of Rights Fund")
NAME_TRUNCATE_WORDS = {'for', 'in', 'and', 'or', 'to', 'at', 'by', 'with'}


def clean_name(raw):
    """Normalizes a candidate name: strip titles, office suffixes, noise, title-case."""
    name = re.sub(r'\s+', ' ', raw).strip()
    name = re.sub(r'[\'\"]+$', '', name).strip()
    # Strip title prefix (Assemblywoman, Congressman, etc.)
    name = TITLE_PREFIX_RE.sub('', name).strip()
    # Strip trailing office suffix
    name = OFFICE_SUFFIX_RE.sub('', name).strip()
    # Truncate at connector words — prevents "Luis Terrazas For New" → keep "Luis Terrazas"
    words = name.split()
    clean_words = []
    for w in words:
        if w.lower() in NAME_TRUNCATE_WORDS:
            break
        clean_words.append(w)
    name = ' '.join(clean_words)
    # Strip trailing punctuation
    name = re.sub(r'[,\.\-:]+$', '', name).strip()
    return name.title() if name else ''


def extract_from_keyword_mappings(mappings):
    """
    Tier 1: keyword_mappings where mapped_canonical_term = "Name (Exact)"
    AND mapped_subcategory = "Candidate Elements" (not Opposition Elements).
    Returns (name, "HIGH") or (None, None).
    """
    candidates = []
    opponent_names = set()

    for entry in mappings:
        term = (entry.get("verbatim_term") or "").strip()
        category = entry.get("mapped_primary_category") or ""
        subcategory = entry.get("mapped_subcategory") or ""
        canonical = entry.get("mapped_canonical_term") or ""

        if category != "Candidate & Entity Identifiers":
            continue

        # Track opponent names so we don't accidentally return them
        if subcategory == "Opposition Elements":
            if len(term.split()) >= 2:
                opponent_names.add(clean_name(term).lower())
            continue

        # Candidate Elements with Name (Exact)
        if subcategory == "Candidate Elements" and canonical == "Name (Exact)":
            if len(term.split()) >= 2:
                candidates.append(clean_name(term))

    if not candidates:
        return None, None

    # Filter out any name that also appears as an opponent
    candidates = [c for c in candidates if c.lower() not in opponent_names]
    if not candidates:
        return None, None

    freq = Counter(candidates)
    best = freq.most_common(1)[0][0]
    return best, "HIGH"


def extract_from_summary(summary):
    """
    Tier 2: Parse summary text for candidate name patterns.
    Returns (name, confidence) or (None, None).
    """
    if not summary:
        return None, None

    for pattern, confidence in SUMMARY_PATTERNS:
        m = pattern.search(summary)
        if m:
            name = clean_name(m.group(1))
            words = name.lower().split()
            # Reject if any word is a generic/disqualifying term
            if any(w in SUMMARY_REJECT_WORDS for w in words):
                continue
            if len(name.split()) >= 2:
                return name, confidence

    return None, None


def extract_client_from_ai(ai_json_str):
    """
    Full extraction pipeline for one document.
    Returns (client_name, confidence, source_tier).
    """
    if not ai_json_str or ai_json_str.strip() in ('', 'NULL', 'None'):
        return None, None, None

    try:
        data = json.loads(ai_json_str)
    except (json.JSONDecodeError, TypeError):
        return None, None, "parse_error"

    mappings = data.get("keyword_mappings") or []
    summary = data.get("summary") or ""

    # Tier 1: keyword_mappings Name (Exact) — candidate elements only
    name, confidence = extract_from_keyword_mappings(mappings)
    if name:
        return name, confidence, "tier1_keyword"

    # Tier 2: summary patterns (positive/promoting framing)
    name, confidence = extract_from_summary(summary)
    if name:
        return name, confidence, "tier2_summary"

    # Tier 3: attack mailer — extract paying client from summary sponsor language
    # e.g. "A mailer from McSally attacking Mark Kelly..." → McSally
    attack_patterns = [
        re.compile(
            r'(?:mailer|ad|piece|flyer)\s+(?:from|by|for)\s+'
            r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\s+attack',
            re.IGNORECASE
        ),
        # "paid for by / sponsored by [Name/Org]" in summary
        re.compile(
            r'(?:paid\s+for|sponsored)\s+by\s+([A-Z][a-z\s&,]+?)(?:\.|,|\n|$)',
            re.IGNORECASE
        ),
        # "A [Org] mailer" / "[Org] mailer attacking..."
        re.compile(
            r'^([A-Z][A-Za-z\s&]+?)\s+mailer\s+(?:attack|criticiz|oppos)',
            re.IGNORECASE
        ),
    ]
    for pattern in attack_patterns:
        m = pattern.search(summary)
        if m:
            name = clean_name(m.group(1))
            if len(name.split()) >= 2:
                return name, "MEDIUM", "tier3_attack"

    return None, None, "no_match"


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run(csv_path, output_path):
    rows = []
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    print(f"Processing {len(rows)} documents...")

    tier_counts = Counter()
    confidence_counts = Counter()
    results = []

    for row in rows:
        doc_id = row.get("id", "")
        filename = row.get("filename", "")
        ai_json = row.get("ai_analysis", "")

        client, confidence, source = extract_client_from_ai(ai_json)

        tier_counts[source or "no_match"] += 1
        confidence_counts[confidence or "none"] += 1

        results.append({
            "id": doc_id,
            "filename": filename,
            "client_from_ai": client or "",
            "ai_confidence": confidence or "",
            "source_tier": source or "",
        })

    # Write output
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=["id", "filename", "client_from_ai", "ai_confidence", "source_tier"])
        writer.writeheader()
        writer.writerows(results)

    # Summary
    total = len(rows)
    extracted = sum(1 for r in results if r["client_from_ai"])
    print(f"\nResults:")
    print(f"  Extracted:  {extracted} / {total} ({extracted/total:.0%})")
    print(f"  No match:   {total - extracted}")
    print(f"\nBy source tier:")
    for tier, count in tier_counts.most_common():
        print(f"  {tier:<25} {count:>5}")
    print(f"\nBy confidence:")
    for conf, count in confidence_counts.most_common():
        print(f"  {conf:<10} {count:>5}")
    print(f"\nOutput written to: {output_path}")

    # Sample — show first 10 extractions
    print(f"\nSample extractions:")
    shown = 0
    for r in results:
        if r["client_from_ai"] and shown < 10:
            print(f"  [{r['id']:>4}] {r['source_tier']:<25} {r['client_from_ai']}  ({r['ai_confidence']})")
            shown += 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, help="Path to ai_analysis.csv")
    parser.add_argument("--output", default="ai_client_extract.csv", help="Output CSV path")
    args = parser.parse_args()
    run(args.csv, args.output)
