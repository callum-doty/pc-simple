"""
Document Field Extractor v2
Extracts date_created, candidate_name, paid_for_by_raw, and state
from political mail PDFs using a structured extraction → semantic resolution pipeline.

Usage:
    python extract_fields.py --input validation_set.csv --output predictions.csv
"""

import re
import csv
import argparse
from dateutil import parser as dateutil_parser

# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

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
    "Wisconsin": "WI", "Wyoming": "WY", "District of Columbia": "DC",
}

# Words common in political mail slogans/attack copy — not candidate name components
SLOGAN_WORDS = {
    "NEED", "NEEDS", "WORK", "WORKS", "WORKING", "TRUST", "SAFE", "SAFETY",
    "TOLD", "LIES", "LIE", "STOP", "FIGHT", "FIGHTS", "PROTECT", "SAVE",
    "HELP", "HELPS", "FAIL", "FAILS", "FAILED", "RAISE", "RAISED", "COST",
    "COSTS", "WRONG", "RIGHT", "KNOW", "KNEW", "MAKE", "MAKES", "KEEP",
    "KEEPS", "BRING", "BRINGS", "LEAD", "LEADS", "STAND", "STANDS", "BOLD",
    "WANT", "WANTS", "CARE", "CARES", "GIVE", "GIVES", "TAKE", "TAKES",
    "PLAN", "PLANS", "MORE", "LESS", "REAL", "TRUE", "JUST", "ONLY",
}

# Words that appear all-caps in political mail but are NOT candidate names
GENERIC_CAPS_WORDS = {
    "VOTE", "FOR", "STATE", "ELECT", "THE", "AND", "OR", "IN", "OF", "TO",
    "A", "AN", "IS", "ON", "AT", "BY", "US", "OUR", "WE", "YOU", "HE", "SHE",
    "IT", "NOT", "NO", "YES", "PAID", "AUTHORIZED", "COMMITTEE", "REPUBLICAN",
    "DEMOCRAT", "DEMOCRATIC", "PARTY", "CONGRESS", "SENATE", "HOUSE", "ASSEMBLY",
    "GOVERNOR", "PRESIDENT", "POLITICAL", "AD", "OFFICIAL", "FUNDS", "PRIMARY",
    "ELECTION", "GENERAL", "DISTRICT", "COUNTY", "CITY", "JOIN", "SIGN", "SEND",
    "STAND", "FIGHT", "BACK", "UP", "NOW", "READY", "TRUST", "RESULTS",
    "CONSERVATIVE", "LIBERAL", "FREEDOM", "LIBERTY", "AMERICA", "AMERICAN",
    "NORTH", "SOUTH", "EAST", "WEST", "UNITED", "STATES", "FEDERAL", "LOCAL",
    "PUBLIC", "SAFE", "SAFETY", "SECURE", "BORDER", "TAX", "TAXES", "CRIME",
    "BALLOT", "EARLY", "REFORM", "PLAN", "POLICY", "PEOPLE", "COMMUNITY",
    "PAID", "PRESORT", "PRSRT", "POSTAGE", "PERMIT", "STD", "PRST",
}

FRANK_INDICATORS = [
    "official funds authorized by the house",
    "official funds authorized by the senate",
    "official funds authorized by the",   # catches truncated variants
    "official funds of",
]

# ---------------------------------------------------------------------------
# DATE EXTRACTION (scoring-based)
# ---------------------------------------------------------------------------

DATE_REGEX = re.compile(
    r'\b(\d{1,2}/\d{1,2}/\d{2,4})\s+(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?)',
    re.IGNORECASE
)


def score_date_candidate(text, start_pos, match_str):
    """Score a date candidate based on surrounding context."""
    score = 0
    pre = text[max(0, start_pos - 120):start_pos]

    # Strong positive: near an .indd filename stamp
    if re.search(r'\.indd', pre, re.IGNORECASE):
        score += 3

    # Positive: has time component
    if re.search(r'\d{1,2}:\d{2}', match_str):
        score += 2

    # Positive: has AM/PM
    if re.search(r'[AP]M', match_str, re.IGNORECASE):
        score += 1

    # Positive: same date appears more than once (consistent across pages)
    date_part = match_str.split()[0]
    if text.count(date_part) > 1:
        score += 1

    # Negative: preceded by sentence-level connectors (likely body copy)
    if re.search(r'\b(on|by|since|as of|from|dated|through|until|after|before)\s*$',
                 pre.strip(), re.IGNORECASE):
        score -= 2

    # Negative: in a citation/source context
    if re.search(r'(source|footnote|\d+\]|\(\d)', pre[-60:], re.IGNORECASE):
        score -= 2

    return score


def extract_date(text):
    """
    Scores all date candidates and picks the highest-scoring one.
    Returns (date_string | None, confidence | None).
    """
    candidates = []
    for m in DATE_REGEX.finditer(text):
        full = m.group(0)
        score = score_date_candidate(text, m.start(), full)
        candidates.append((score, full, m.start()))

    if not candidates:
        return None, None

    best_score, best_match, _ = max(candidates, key=lambda x: x[0])

    if best_score <= 0:
        return None, "LOW"

    try:
        parsed = dateutil_parser.parse(best_match.strip())
        confidence = "HIGH" if best_score >= 3 else "MEDIUM"
        return parsed.strftime("%Y-%m-%d"), confidence
    except Exception:
        return None, "LOW"


# ---------------------------------------------------------------------------
# PAID-FOR-BY EXTRACTION (raw)
# ---------------------------------------------------------------------------

PAID_FOR_BY_REGEX = re.compile(
    r'(?:not\s+authorized[^.]+?\.?\s*)?'
    r'(?:authorized\s+and\s+)?'
    r'(?:political\s+ad\s+)?'
    r'paid\s+for\s+by\s+(.+)',
    re.IGNORECASE
)


def is_congressional_frank(raw):
    lower = raw.lower()
    return any(indicator in lower for indicator in FRANK_INDICATORS)


def strip_treasurer_name(raw):
    """
    Strips trailing treasurer/agent person names after a comma or period.
    Handles:
      - ", Jane Timken, Chairman"
      - ", Jennifer Donnelly, Treasurer"
      - ". Registered Agent: Kate Kennedy"
      - ". Katie Kennedy, Registered Agent. Not authorized..."
      - ", JON ANDERSON, REGISTERED AGENT. NOT AUTHORIZED..."
    """
    # Strip IEC/PAC "not authorized" disclaimer (period or comma separated)
    cleaned = re.sub(
        r'[\.,]\s*not\s+authorized\s+by\s+any\s+candidate.*$',
        '', raw, flags=re.IGNORECASE
    ).strip()

    # Strip ". Registered Agent: [Name]" or ". [Name], Registered Agent"
    cleaned = re.sub(
        r'\.\s*(?:registered\s+agent\s*:?\s*\S.*|[A-Z][A-Za-z]+\s+[A-Z][A-Za-z]+,?\s*registered\s+agent.*)$',
        '', cleaned, flags=re.IGNORECASE
    ).strip()

    # Strip comma-separated person name + optional role (mixed or ALL CAPS)
    cleaned = re.sub(
        r',\s*[A-Z][A-Za-z]+\s+[A-Z][A-Za-z]+(?:\s*,?\s*(?:Treasurer|Chairman|Chair|'
        r'Secretary|Registered\s+Agent|Agent|Director|President))?\s*$',
        '', cleaned, flags=re.IGNORECASE
    ).strip()

    # ALL CAPS version: ", JON ANDERSON, REGISTERED AGENT"
    cleaned = re.sub(
        r',\s*[A-Z]{2,}\s+[A-Z]{2,}(?:\s*,?\s*(?:TREASURER|CHAIRMAN|CHAIR|'
        r'SECRETARY|REGISTERED\s+AGENT|AGENT|DIRECTOR|PRESIDENT))?\s*$',
        '', cleaned
    ).strip()

    # Standalone role word after comma e.g. ", Treasurer"
    cleaned = re.sub(
        r',\s*(?:Treasurer|Chairman|Chair|Secretary|Registered\s+Agent|Agent|Director)\s*$',
        '', cleaned, flags=re.IGNORECASE
    ).strip()

    return cleaned


def extract_paid_for_by(text):
    """
    Returns (raw_string | None, is_frank: bool).
    Extracts first paid-for-by occurrence, cleaned of address/noise lines and treasurer names.
    """
    matches = list(PAID_FOR_BY_REGEX.finditer(text))
    if not matches:
        return None, False

    raw = matches[0].group(1).strip()

    if is_congressional_frank(raw):
        return raw, True

    # Take only the first line (committee name, before address block)
    raw = raw.split('\n')[0].strip()

    # Strip pipe-delimited address suffixes (e.g. "Julia Palzer For Midtown | 1325 S. 36th...")
    raw = re.sub(r'\s*\|.*$', '', raw).strip()

    # Strip FPPC/PAC IDs
    raw = re.sub(r'\s*\|\s*(?:FPPC|PAC).*$', '', raw, flags=re.IGNORECASE)

    # Strip trailing punctuation noise
    raw = re.sub(r'[.*]+$', '', raw).strip()

    # Strip "and" conjunction artifact left from multi-party paid-for-by lines
    raw = re.sub(r'\s+and\s*$', '', raw, flags=re.IGNORECASE).strip()

    # Strip treasurer/agent/chairman person names after comma or period
    raw = strip_treasurer_name(raw)

    return raw, False


# ---------------------------------------------------------------------------
# CANDIDATE NAME EXTRACTION (heuristic layers)
# ---------------------------------------------------------------------------

# Org-signal words — if present, the string is likely a party/PAC, not a person
ORG_SIGNAL_REGEX = re.compile(
    r'\b(party|republican|democrat|democratic|pac|fund|action|leadership'
    r'|coalition|association|future|strong|alliance|patriots|citizens'
    r'|victory|freedom|liberty|values|trust|neighbors|families|voters'
    r'|workers|taxpayers|teachers|parents|seniors|veterans|iec)\b',
    re.IGNORECASE
)

# Office keywords used to strip from committee names
OFFICE_KEYWORDS = (
    r'congress|u\.?s\.?\s+congress|senate|u\.?s\.?\s+senate|assembly'
    r'|governor|attorney\s+general|state\s+(?:representative|rep|senate|senator)'
    r'|county|district|judge|sheriff|mayor|city\s+council|school\s+board'
    r'|treasurer|comptroller|clerk|commissioner|house|representative'
    r'|[a-z]+\s+\d+'
)


def committee_to_candidate(committee_name):
    """
    Strips committee wrappers to extract a bare candidate name.
    Returns candidate name string, or None if it looks like a party/PAC.
    """
    name = committee_name.strip()

    # Remove leading wrappers
    name = re.sub(
        r'^(?:'
        r'(?:the\s+)?committee\s+to\s+(?:re-?)?elect\s+'   # "The Committee To Elect"
        r'|(?:the\s+)?(?:re-?)?elect\s+'                   # "Re-Elect" / "The Elect"
        r'|friends?\s+of\s+'                               # "Friends of"
        r')',
        '', name, flags=re.IGNORECASE
    ).strip()

    # Remove trailing office + optional location prefix + year
    # Handles: "for Congress", "for Atascadero Mayor", "for GCISD School Board",
    #          "for Iberia Parish Sheriff", "for Texas 12", etc.
    name = re.sub(
        rf'\s+(?:for\s+(?:\w+\s+)*(?:{OFFICE_KEYWORDS})|\bcampaign\b|\bpac\b|\bfund\b|\bcommittee\b)\s*\d*\s*$',
        '', name, flags=re.IGNORECASE
    ).strip()

    # Strip bare "for [State/Location]" suffixes — but only if result stays >= 2 words
    bare_state_stripped = re.sub(
        r'\s+for\s+(?:[A-Z]{2}|\w+)\s*(?:\(R\)|\(D\)|\d+)?\s*$',
        '', name, flags=re.IGNORECASE
    ).strip()
    if len(bare_state_stripped.split()) >= 2:
        name = bare_state_stripped

    # Strip trailing party affiliation suffix e.g. "(R)" or "(D)"
    name = re.sub(r'\s*\([RD]\)\s*$', '', name, flags=re.IGNORECASE).strip()
    # Strip trailing year
    name = re.sub(r'\s+\d{4}\s*$', '', name).strip()

    # If org-signal words remain, this is a party/PAC not a person
    if ORG_SIGNAL_REGEX.search(name):
        return None

    # Must be at least 2 words to be a valid name
    if len(name.split()) < 2:
        return None

    return name if name else None


def extract_candidate_from_document(text):
    """
    Scans document body for candidate name signals.
    Returns (candidate_name | None, confidence).
    """
    # Layer 1: "Vote/Re-Elect/Elect [Name]" — most reliable signal
    # Require name to be >= 2 words and appear BEFORE any "for [office]" clause
    vote_patterns = [
        # Mixed case: "Vote Heather Moreno for Mayor" → "Heather Moreno"
        r'(?:Vote|Re-?Elect|Elect)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})(?=\s+(?:for\b|$|\n))',
        # All-caps: "VOTE CRAIG TIPPING FOR..." → "CRAIG TIPPING"
        r'(?:VOTE|RE-?ELECT|ELECT)\s+([A-Z]{2,}(?:\s+[A-Z]{2,}){1,2})(?=\s+FOR|\s*\n)',
    ]
    for pattern in vote_patterns:
        m = re.search(pattern, text)
        if m:
            candidate = m.group(1).strip().title()
            if len(candidate.split()) >= 2:
                return candidate, "HIGH"

    # Layer 2: Frequency + position heuristic
    # Candidate names repeat across pages; slogans typically don't.
    # Find all 2-3 word all-caps phrases, count occurrences, weight by early position.
    all_caps_candidates = re.findall(
        r'\b([A-Z]{3,}(?:\s+[A-Z]{3,}){1,2})\b', text
    )
    counts = {}
    for phrase in all_caps_candidates:
        phrase = re.sub(r'\s+', ' ', phrase).strip()
        words = phrase.split()
        if (2 <= len(words) <= 3
                and not any(w in GENERIC_CAPS_WORDS for w in words)
                and not ORG_SIGNAL_REGEX.search(phrase)):
            counts[phrase] = counts.get(phrase, 0) + 1

    if counts:
        # Rank by frequency; break ties by earliest appearance in document
        def rank_key(phrase):
            freq = counts[phrase]
            pos = text.find(phrase)
            early_bonus = 1 if pos < 800 else 0
            return (freq + early_bonus, -pos)

        best = max(counts, key=rank_key)
        # Reject if any word in the best phrase is a slogan word
        if not any(w in SLOGAN_WORDS for w in best.split()):
            if counts[best] >= 1:
                confidence = "HIGH" if counts[best] >= 2 else "MEDIUM"
                return best.title(), confidence

    # Layer 3: "[Name] for [Office]" — name must be >= 2 words, each >= 4 chars
    # Minimum char length prevents matching short words like "For", "Jim", "of"
    m = re.search(
        r'(?<!\bfor\s)(?<!\bvote\s)'
        r'\b([A-Z][a-z]{3,}\s+[A-Z][a-z]{3,}(?:\s+[A-Z][a-z]{3,})?)\s+[Ff]or\s+'
        r'(?:Congress|Senate|Assembly|Governor|Attorney|State|County|District|Mayor|Judge|School)',
        text
    )
    if m:
        candidate = m.group(1).strip()
        if len(candidate.split()) >= 2:
            return candidate, "MEDIUM"

    # Layer 4: Domain hint (e.g. "JenForCongress.com" → "Jen")
    m = re.search(r'(?:www\.)?([A-Z][a-z]{2,})(?:For|4)[A-Z]', text)
    if m:
        return m.group(1), "LOW"

    return None, None


def extract_client(text):
    """
    Two-stage pipeline:
      Stage 1 — Extract paid_for_by_raw
      Stage 2 — Resolve to candidate_name via committee normalization + doc heuristics

    Returns (candidate_name, confidence, paid_for_by_raw, is_frank)
    """
    paid_for_by_raw, is_frank = extract_paid_for_by(text)

    if is_frank:
        candidate, confidence = extract_candidate_from_document(text)
        return candidate, "LOW", paid_for_by_raw, True

    # Try committee name → candidate name normalization
    if paid_for_by_raw:
        candidate = committee_to_candidate(paid_for_by_raw)
        if candidate:
            # Validate: candidate's first name should appear somewhere in the document
            first_name = candidate.split()[0]
            confidence = "HIGH" if first_name.lower() in text.lower() else "MEDIUM"
            return candidate, confidence, paid_for_by_raw, False

    # Short names (≤ 4 words) → likely a short candidate name or short org — return as-is
    if paid_for_by_raw and len(paid_for_by_raw.split()) <= 4:
        return paid_for_by_raw, "MEDIUM", paid_for_by_raw, False

    # Longer party/org — fall back to document-level heuristics for candidate
    doc_candidate, doc_confidence = extract_candidate_from_document(text)
    if doc_candidate:
        return doc_candidate, doc_confidence, paid_for_by_raw, False

    # Last resort: return raw string at LOW confidence
    if paid_for_by_raw:
        return paid_for_by_raw, "LOW", paid_for_by_raw, False

    return None, None, None, False


# ---------------------------------------------------------------------------
# STATE EXTRACTION
# ---------------------------------------------------------------------------

STATE_ABBR_REGEX = re.compile(r',\s*([A-Z]{2})\s+\d{5}')

STATE_SPELLED_REGEX = re.compile(
    r'\b(' + '|'.join(re.escape(s) for s in sorted(STATE_NAME_TO_ABBR, key=len, reverse=True)) + r')\b',
    re.IGNORECASE
)

# Race context patterns: "for [State]" or "[State] [Office]"
RACE_STATE_PATTERNS = []
for _state_name, _abbr in STATE_NAME_TO_ABBR.items():
    RACE_STATE_PATTERNS.append((
        re.compile(
            rf'\bfor\s+{re.escape(_state_name)}\b'
            rf'|\b{re.escape(_state_name)}\s+(?:senate|assembly|governor|house|congress|district|representative|state\s+rep)\b',
            re.IGNORECASE
        ),
        _abbr
    ))

# Tier 0: Header dominance — office/district patterns that imply a specific state
# Ordered most-specific → least-specific
HEADER_STATE_PATTERNS = [
    # California: "STATE ASSEMBLY [number]" (numbered districts are CA-specific in context)
    (re.compile(r'\bSTATE\s+ASSEMBLY\s+\d+\b'), "CA"),
    # Texas indicators
    (re.compile(r'\bHPISD\b|\bGCISD\b|\bFORT\s+WORTH\b|\bCISD\b'), "TX"),
    # Nebraska indicators
    (re.compile(r'\bLINCOLN,\s*NE\b|\bOMAHA,\s*NE\b'), "NE"),
    # Oregon indicators
    (re.compile(r'\bOREGON\s+CITY\b|\bPORTLAND,\s*OR\b'), "OR"),
    # New Mexico
    (re.compile(r'\bALBUQUERQUE\b|\bSANTA\s+FE,\s*NM\b'), "NM"),
    # Missouri
    (re.compile(r'\bJEFFERSON\s+CITY,\s*MO\b|\bST\.\s+LOUIS,\s*MO\b'), "MO"),
    # Ohio
    (re.compile(r'\bCOLUMBUS,\s*OH\b|\bCINCINNATI,\s*OH\b'), "OH"),
]


def state_frequency_count(text):
    """Returns the most frequently mentioned state name abbreviation in the document."""
    counts = {}
    for state_name, abbr in STATE_NAME_TO_ABBR.items():
        count = len(re.findall(rf'\b{re.escape(state_name)}\b', text, re.IGNORECASE))
        if count > 0:
            counts[abbr] = counts.get(abbr, 0) + count
    return (max(counts, key=counts.get), counts) if counts else (None, {})


def extract_state(text):
    """
    Tier 0: Header dominance — office/district patterns implying a specific state
    Tier 1: ZIP-based abbreviation from address block after paid-for-by
    Tier 2: Spelled-out state name in same address block
    Tier 3: Race context patterns ("for Texas", "Texas Senate", etc.)
    Tier 4: State frequency count across full document
    Returns (state_abbr | None, confidence | None).
    """
    # Tier 0: Header dominance (highest signal — these patterns are state-unique)
    for pattern, abbr in HEADER_STATE_PATTERNS:
        if pattern.search(text):
            return abbr, "HIGH"

    # Narrow address search to paid-for-by section
    # Cap at first blank line (natural end of address block) or 600 chars max
    paid_match = re.search(r'paid\s+for\s+by', text, re.IGNORECASE)
    if paid_match:
        after_paid = text[paid_match.start():]
        blank_line = re.search(r'\n\s*\n', after_paid)
        # Floor of 150 chars — prevents blank line snapping window shut before ZIP is reached
        window = blank_line.start() if blank_line and 150 < blank_line.start() < 600 else 600
        address_block = after_paid[:window]
    else:
        address_block = text

    # Tier 1: ZIP-based abbreviation
    m = STATE_ABBR_REGEX.search(address_block)
    if m:
        return m.group(1).upper(), "HIGH"

    # Tier 2: Spelled-out state name in address block
    m = STATE_SPELLED_REGEX.search(address_block)
    if m:
        for name, abbr in STATE_NAME_TO_ABBR.items():
            if name.lower() == m.group(1).lower():
                return abbr, "MEDIUM"

    # Tier 3: Race context in full document
    for pattern, abbr in RACE_STATE_PATTERNS:
        if pattern.search(text):
            return abbr, "MEDIUM"

    # Tier 4: Frequency count across document
    most_frequent, counts = state_frequency_count(text)
    if most_frequent and counts[most_frequent] >= 2:
        return most_frequent, "LOW"

    return None, None


# ---------------------------------------------------------------------------
# MAIN PIPELINE
# ---------------------------------------------------------------------------

def process_document(text):
    date, date_conf = extract_date(text)
    candidate, candidate_conf, paid_for_by_raw, is_frank = extract_client(text)
    state, state_conf = extract_state(text)

    return {
        "predicted_date": date,
        "date_confidence": date_conf,
        "predicted_candidate": candidate,
        "candidate_confidence": candidate_conf,
        "paid_for_by_raw": paid_for_by_raw,
        "is_frank": is_frank,
        "predicted_state": state,
        "state_confidence": state_conf,
    }


CONFIDENCE_WEIGHTS = {
    "date_confidence":      {"HIGH": 2, "MEDIUM": 1, "LOW": 0, None: 0},
    "candidate_confidence": {"HIGH": 3, "MEDIUM": 1, "LOW": 0, None: 0},
    "state_confidence":     {"HIGH": 2, "MEDIUM": 1, "LOW": 0, None: 0},
}
REVIEW_THRESHOLD = 5   # max possible = 7; below this triggers review


def confidence_score(fields):
    """
    Weighted confidence score across all three fields.
    date:      HIGH=2  MEDIUM=1  LOW/None=0
    candidate: HIGH=3  MEDIUM=1  LOW/None=0
    state:     HIGH=2  MEDIUM=1  LOW/None=0
    Max = 7. Threshold for needs_review = < 5.
    """
    score = 0
    for key, weights in CONFIDENCE_WEIGHTS.items():
        val = fields.get(key)
        score += weights.get(val, 0)
    return score


def needs_review_flag(fields):
    """
    Returns True if weighted confidence score is below threshold,
    or if any critical field (candidate, state) is missing entirely.
    Weighted scoring prevents over-flagging rows with one MEDIUM field
    and under-flagging rows with multiple LOW/missing fields.
    """
    if not fields.get("predicted_candidate") or not fields.get("predicted_state"):
        return True
    return confidence_score(fields) < REVIEW_THRESHOLD


def run(input_path, output_path, mode="validation"):
    """
    mode="validation" — includes actual_*/correct columns for manual scoring
    mode="production" — clean output ready for DB import, includes needs_review flag
    """
    if mode == "production":
        output_fields = [
            "id", "filename",
            "date_created", "date_confidence",
            "client", "client_confidence",
            "paid_for_by_raw", "is_frank",
            "state", "state_confidence",
            "needs_review",
        ]
    else:
        output_fields = [
            "id", "filename",
            "predicted_date", "date_confidence",
            "predicted_candidate", "candidate_confidence",
            "paid_for_by_raw", "is_frank",
            "predicted_state", "state_confidence",
            "actual_date", "actual_candidate", "actual_state",
            "date_correct", "candidate_correct", "state_correct",
            "notes",
        ]

    with open(input_path, newline='', encoding='utf-8') as infile, \
         open(output_path, 'w', newline='', encoding='utf-8') as outfile:

        reader = csv.DictReader(infile)
        writer = csv.DictWriter(outfile, fieldnames=output_fields)
        writer.writeheader()

        total = 0
        flagged = 0

        for row in reader:
            doc_id = row.get("id", "")
            filename = row.get("filename", "")
            text = row.get("extracted_text", "")

            fields = process_document(text)
            review = needs_review_flag(fields)
            if review:
                flagged += 1
            total += 1

            if mode == "production":
                writer.writerow({
                    "id": doc_id,
                    "filename": filename,
                    "date_created": fields["predicted_date"],
                    "date_confidence": fields["date_confidence"],
                    "client": fields["predicted_candidate"],
                    "client_confidence": fields["candidate_confidence"],
                    "paid_for_by_raw": fields["paid_for_by_raw"],
                    "is_frank": fields["is_frank"],
                    "state": fields["predicted_state"],
                    "state_confidence": fields["state_confidence"],
                    "needs_review": review,
                })
            else:
                writer.writerow({
                    "id": doc_id,
                    "filename": filename,
                    **fields,
                    "actual_date": "",
                    "actual_candidate": "",
                    "actual_state": "",
                    "date_correct": "",
                    "candidate_correct": "",
                    "state_correct": "",
                    "notes": "",
                })

            status = "⚑ REVIEW" if review else "✓"
            print(f"[{doc_id}] {status} {filename}")
            print(f"  date:      {fields['predicted_date']} ({fields['date_confidence']})")
            print(f"  client:    {fields['predicted_candidate']} ({fields['candidate_confidence']})")
            print(f"  state:     {fields['predicted_state']} ({fields['state_confidence']})")
            print()

    print(f"Done. {total} documents processed, {flagged} flagged for review.")
    print(f"Output written to: {output_path}")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument("--input", required=True, help="Path to input CSV")
    arg_parser.add_argument("--output", required=True, help="Path to output CSV")
    arg_parser.add_argument("--mode", default="validation", choices=["validation", "production"],
                            help="'validation' includes scoring columns; 'production' outputs DB-ready CSV")
    args = arg_parser.parse_args()
    run(args.input, args.output, mode=args.mode)
