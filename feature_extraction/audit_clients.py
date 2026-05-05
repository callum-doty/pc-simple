"""
Client Name Dedup Audit
Analyzes all_predictions.csv to surface duplicate and near-duplicate client names
before committing to the database.

Usage:
    python audit_clients.py --csv all_predictions.csv --output client_audit.csv
"""

import csv
import re
import argparse
from collections import defaultdict


def normalize_for_compare(name):
    """Lowercase, strip leading 'the', collapse whitespace, strip punctuation."""
    if not name:
        return ""
    n = name.lower().strip()
    n = re.sub(r'^the\s+', '', n)
    n = re.sub(r'[^\w\s]', '', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n


def simple_similarity(a, b):
    """
    Basic token overlap ratio — catches:
      'California Republican Party' vs 'CA Republican Party'
      'Daniel Cameron' vs 'Daniel W Cameron'
    Returns 0.0–1.0.
    """
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


def run(csv_path, output_path, similarity_threshold=0.6):
    # Load all client names
    clients = defaultdict(list)  # normalized_name -> [raw names]
    raw_counts = defaultdict(int)  # raw name -> count

    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            client = (row.get("client") or "").strip()
            if not client:
                continue
            raw_counts[client] += 1
            norm = normalize_for_compare(client)
            clients[norm].append(client)

    # --- Report 1: Exact normalized duplicates ---
    print("\n=== EXACT NORMALIZED DUPLICATES ===")
    print("(Same name, different casing/punctuation/'the' prefix)\n")
    exact_dupes = {k: v for k, v in clients.items() if len(set(v)) > 1}
    for norm, raw_list in sorted(exact_dupes.items()):
        unique_raws = sorted(set(raw_list))
        total = sum(raw_counts[r] for r in unique_raws)
        print(f"  [{total} docs]  {' | '.join(unique_raws)}")

    print(f"\n  {len(exact_dupes)} groups found.\n")

    # --- Report 2: Near-duplicates by token overlap ---
    print("=== NEAR-DUPLICATE GROUPS (token similarity >= {:.0%}) ===".format(similarity_threshold))
    print("(Different names that likely refer to the same client)\n")

    norm_keys = list(clients.keys())
    visited = set()
    near_dupe_groups = []

    for i, a in enumerate(norm_keys):
        if a in visited:
            continue
        group = [a]
        for j, b in enumerate(norm_keys):
            if i == j or b in visited:
                continue
            if simple_similarity(a, b) >= similarity_threshold:
                group.append(b)
        if len(group) > 1:
            visited.update(group)
            near_dupe_groups.append(group)

    for group in sorted(near_dupe_groups, key=lambda g: -sum(raw_counts[r] for norm in g for r in set(clients[norm]))):
        all_raws = []
        for norm in group:
            all_raws.extend(set(clients[norm]))
        total = sum(raw_counts[r] for r in all_raws)
        print(f"  [{total} docs]  {' | '.join(sorted(set(all_raws)))}")

    print(f"\n  {len(near_dupe_groups)} groups found.\n")

    # --- Report 3: Full frequency table → CSV ---
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(["client_raw", "count", "normalized"])
        for raw, count in sorted(raw_counts.items(), key=lambda x: -x[1]):
            writer.writerow([raw, count, normalize_for_compare(raw)])

    print(f"Full client frequency table written to: {output_path}")
    print(f"({len(raw_counts)} unique raw client names across all documents)\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, help="Path to all_predictions.csv")
    parser.add_argument("--output", default="client_audit.csv", help="Output frequency table CSV")
    parser.add_argument("--threshold", type=float, default=0.6,
                        help="Token similarity threshold for near-duplicate detection (default: 0.6)")
    args = parser.parse_args()
    run(args.csv, args.output, similarity_threshold=args.threshold)
