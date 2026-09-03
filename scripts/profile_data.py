"""
Read-only data profiler — measures what is actually POPULATED, not just what exists.

The data catalog documents every column, its writer, and its semantics. What it
cannot say is how much of each column is filled in, and that decides most
dashboard design questions: whether client_canonical is a headline dimension or
a footnote depends entirely on whether it is 95% populated or 12%.

This script answers that. It issues SELECT statements only — no writes, no DDL,
no locks beyond a read snapshot. Safe to run against production.

Usage:
    DATABASE_URL=postgresql://... python -m scripts.profile_data
    DATABASE_URL=postgresql://... python -m scripts.profile_data --json > profile.json

Output is a plain-text report by default; --json emits the same data as a single
object for pasting back into a conversation.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, text


# ---------------------------------------------------------------------------
# Column groups mirror the catalog's sections so the report reads in the same
# order as the document it is filling in.
# ---------------------------------------------------------------------------

COLUMN_GROUPS = {
    "identity & file": [
        "filename",
        "file_path",
        "file_size",
    ],
    "processing lifecycle": [
        "status",
        "processing_progress",
        "processing_error",
        "created_at",
        "updated_at",
        "processing_started_at",
        "processed_at",
        "processing_heartbeat_at",
    ],
    "content & AI": [
        "extracted_text",
        "ai_analysis",
        "keywords",
        "file_metadata",
    ],
    "search & embeddings": [
        "search_content",
        "search_vector",
        "embedding_model",
        "embedding_version",
        "embedding_provenance",
    ],
    "presentation": [
        "preview_url",
        "thumbnail_url",
    ],
    "ingestion": [
        "dropbox_file_id",
        "content_hash",
    ],
    "political metadata": [
        "paid_for_by_raw",
        "client",
        "client_clean_v1",
        "client_canonical",
        "state",
        "date_created",
        "is_frank",
        "date_confidence",
        "client_confidence",
        "state_confidence",
        "needs_review",
        "needs_date_review",
    ],
}

# Columns where the empty string is as meaningful as NULL — the catalog notes
# that extracted_text and state both use both to mean "absent".
EMPTY_STRING_ALSO_ABSENT = {
    "extracted_text",
    "search_content",
    "state",
    "client",
    "client_canonical",
    "client_clean_v1",
    "paid_for_by_raw",
    "processing_error",
    "filename",
    "file_path",
    "embedding_model",
    "preview_url",
    "thumbnail_url",
    "dropbox_file_id",
    "content_hash",
}

# Low-cardinality columns worth enumerating in full rather than counting.
VALUE_DOMAINS = [
    "status",
    "date_confidence",
    "client_confidence",
    "state_confidence",
    "is_frank",
    "needs_review",
    "needs_date_review",
    "embedding_model",
    "embedding_version",
]

TABLES = [
    "documents",
    "search_queries",
    "taxonomy_terms",
    "taxonomy_synonyms",
    "document_taxonomy_map",
    "canonical_overrides",
    "dropbox_sync_state",
    "alembic_version",
]


def _fetch(conn, sql, **params):
    return conn.execute(text(sql), params).fetchall()


def _one(conn, sql, **params):
    row = conn.execute(text(sql), params).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Profiling passes
# ---------------------------------------------------------------------------


def profile_tables(conn):
    """Row count per table. Missing tables are reported, not fatal."""
    out = {}
    for table in TABLES:
        exists = _one(
            conn,
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :t)",
            t=table,
        )
        if not exists:
            out[table] = None  # distinguishes "absent" from "empty"
            continue
        out[table] = _one(conn, f'SELECT COUNT(*) FROM "{table}"')
    return out


def _live_columns(conn, table):
    rows = _fetch(
        conn,
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :t",
        t=table,
    )
    return {r[0]: r[1] for r in rows}


def profile_documents_columns(conn, total):
    """
    Per-column population rate for the documents table.

    Every column in COLUMN_GROUPS is checked against information_schema first,
    so a column the deployed schema has not migrated yet is reported as absent
    rather than crashing the run.
    """
    live = _live_columns(conn, "documents")
    groups = {}

    for group, columns in COLUMN_GROUPS.items():
        entries = []
        for col in columns:
            if col not in live:
                entries.append({"column": col, "present_in_schema": False})
                continue

            if col in EMPTY_STRING_ALSO_ABSENT:
                filled = _one(
                    conn,
                    f'SELECT COUNT(*) FROM documents WHERE "{col}" IS NOT NULL '
                    f'AND TRIM("{col}"::text) <> \'\'',
                )
            else:
                filled = _one(
                    conn, f'SELECT COUNT(*) FROM documents WHERE "{col}" IS NOT NULL'
                )

            entries.append(
                {
                    "column": col,
                    "present_in_schema": True,
                    "type": live[col],
                    "filled": filled,
                    "pct": round(filled / total * 100, 2) if total else 0.0,
                }
            )
        groups[group] = entries

    return groups


def profile_value_domains(conn):
    """Full value enumeration for low-cardinality columns."""
    live = _live_columns(conn, "documents")
    out = {}
    for col in VALUE_DOMAINS:
        if col not in live:
            out[col] = None
            continue
        rows = _fetch(
            conn,
            f'SELECT "{col}"::text AS v, COUNT(*) AS n FROM documents '
            f'GROUP BY "{col}" ORDER BY n DESC',
        )
        out[col] = [{"value": r[0], "count": r[1]} for r in rows]
    return out


def profile_jsonb_keys(conn, total):
    """
    Key-level coverage inside the four JSONB trees.

    The catalog documents 26 nested keys; several are declared in the Pydantic
    schemas but never written, and others are written but undeclared. This pass
    settles which is which from the data itself.
    """
    checks = {
        "ai_analysis": [
            "schema_version",
            "prompt_version",
            "summary",
            "page_count",
            "analysis_type",
            "keyword_mappings",
            "document_analysis",
            "error",
        ],
        "keywords": [
            "schema_version",
            "keywords",
            "categories",
            "keyword_mappings",
            "mapping_count",
            "extraction_timestamp",
        ],
        "file_metadata": [
            "schema_version",
            "page_count",
            "file_type",
            "processing_cost",
            "processing_checkpoint",
            "feature_extraction",
            "ocr_prompt_version",
        ],
        "embedding_provenance": [
            "schema_version",
            "model",
            "version",
            "text_components",
            "generated_at",
        ],
    }

    # jsonb_exists(col, key) rather than the `?` operator: `?` is also the
    # qmark paramstyle marker, so mixing it with a bound parameter in the same
    # statement is a driver-dependent footgun. The function form is equivalent
    # and unambiguous.
    out = {}
    for column, keys in checks.items():
        col_out = {}
        for key in keys:
            n = _one(
                conn,
                f'SELECT COUNT(*) FROM documents WHERE jsonb_exists("{column}", :k)',
                k=key,
            )
            col_out[key] = {
                "present": n,
                "pct": round(n / total * 100, 2) if total else 0.0,
            }
        out[column] = col_out

    # Nested sub-objects worth their own coverage numbers.
    out["file_metadata.processing_cost"] = {
        k: _one(
            conn,
            "SELECT COUNT(*) FROM documents WHERE jsonb_exists("
            "  file_metadata -> 'processing_cost', :k)",
            k=k,
        )
        for k in ("input_tokens", "output_tokens", "provider", "processed_at")
    }
    out["file_metadata.feature_extraction"] = {
        k: _one(
            conn,
            "SELECT COUNT(*) FROM documents WHERE jsonb_exists("
            "  file_metadata -> 'feature_extraction', :k)",
            k=k,
        )
        for k in (
            "canonical_source",
            "rules_source",
            "ai_confidence",
            "ai_tier",
            "extraction_version",
            "date_source",
            "state_source",
        )
    }
    return out


def profile_hazards(conn, present):
    """
    One direct measurement per hazard in the catalog. Each entry is phrased so
    the number alone says whether the hazard is live in this dataset.

    `present` maps table name -> row count (None when the table is absent), so
    checks against side tables can be skipped rather than raising.
    """
    h = {}

    h["pending_rows_remaining"] = {
        "note": "PENDING has no producer; non-zero means historical rows survive",
        "value": _one(conn, "SELECT COUNT(*) FROM documents WHERE status = 'PENDING'"),
    }
    h["queued_backlog"] = {
        "note": "What the dashboard's queue_depth SHOULD be counting",
        "value": _one(conn, "SELECT COUNT(*) FROM documents WHERE status = 'QUEUED'"),
    }
    h["failed_with_processed_at"] = {
        "note": "FAILED rows carrying processed_at. 0 confirms success rate is pinned to 100%",
        "value": _one(
            conn,
            "SELECT COUNT(*) FROM documents "
            "WHERE status = 'FAILED' AND processed_at IS NOT NULL",
        ),
    }
    h["failed_total"] = {
        "note": "Failures that the 7-day success rate cannot see",
        "value": _one(conn, "SELECT COUNT(*) FROM documents WHERE status = 'FAILED'"),
    }
    h["completed_missing_vector"] = {
        "note": "COMPLETED but never embedded — routine, not failure (separate task)",
        "value": _one(
            conn,
            "SELECT COUNT(*) FROM documents "
            "WHERE status = 'COMPLETED' AND search_vector IS NULL",
        ),
    }
    h["placeholder_summaries"] = {
        "note": "ai_analysis present but summary is the 'No summary available' placeholder",
        "value": _one(
            conn,
            "SELECT COUNT(*) FROM documents "
            "WHERE ai_analysis -> 'summary' IS NOT NULL "
            "AND ai_analysis ->> 'summary' ILIKE '%no summary available%'",
        ),
    }
    h["error_key_in_analysis"] = {
        "note": "Documents whose AI analysis recorded an error key",
        "value": _one(
            conn,
            "SELECT COUNT(*) FROM documents WHERE jsonb_exists(ai_analysis, 'error')",
        ),
    }
    h["processing_error_but_completed"] = {
        "note": "processing_error is never cleared — these are 'ever failed', not broken",
        "value": _one(
            conn,
            "SELECT COUNT(*) FROM documents "
            "WHERE processing_error IS NOT NULL AND status = 'COMPLETED'",
        ),
    }
    h["untrimmed_state"] = {
        "note": "Values needing TRIM; non-zero means naive GROUP BY state splits rows",
        "value": _one(
            conn,
            "SELECT COUNT(*) FROM documents "
            "WHERE state IS NOT NULL AND state <> TRIM(state)",
        ),
    }
    h["distinct_state_raw_vs_trimmed"] = {
        "note": "If these differ, the split above is already affecting charts",
        "value": {
            "raw": _one(
                conn,
                "SELECT COUNT(DISTINCT state) FROM documents WHERE state IS NOT NULL",
            ),
            "trimmed": _one(
                conn,
                "SELECT COUNT(DISTINCT TRIM(state)) FROM documents "
                "WHERE state IS NOT NULL AND TRIM(state) <> ''",
            ),
        },
    }
    h["is_frank_null_vs_false"] = {
        "note": "default=False makes 'unknown' look like 'not franked'",
        "value": {
            "null": _one(conn, "SELECT COUNT(*) FROM documents WHERE is_frank IS NULL"),
            "false": _one(
                conn, "SELECT COUNT(*) FROM documents WHERE is_frank IS FALSE"
            ),
            "true": _one(
                conn, "SELECT COUNT(*) FROM documents WHERE is_frank IS TRUE"
            ),
        },
    }
    h["frank_pct_all_rows_vs_extracted"] = {
        "note": "The gap between these two is the understatement the catalog warns about",
        "value": _fetch(
            conn,
            """
            SELECT
                ROUND(COUNT(*) FILTER (WHERE is_frank) * 100.0
                      / NULLIF(COUNT(*), 0), 2)                        AS pct_all_rows,
                ROUND(COUNT(*) FILTER (WHERE is_frank) * 100.0
                      / NULLIF(COUNT(*) FILTER
                        (WHERE client_canonical IS NOT NULL), 0), 2)   AS pct_extracted_only
            FROM documents
            """,
        )[0]._asdict(),
    }
    h["confidence_null_counts"] = {
        "note": "Rows the confidence bars silently drop (isnot(None) filter)",
        "value": {
            c: _one(conn, f"SELECT COUNT(*) FROM documents WHERE {c} IS NULL")
            for c in ("date_confidence", "client_confidence", "state_confidence")
        },
    }
    h["needs_date_review_vs_missing_date"] = {
        "note": "Flag counts range violations only; missing dates are far more numerous",
        "value": {
            "flagged": _one(
                conn, "SELECT COUNT(*) FROM documents WHERE needs_date_review IS TRUE"
            ),
            "date_created_null": _one(
                conn, "SELECT COUNT(*) FROM documents WHERE date_created IS NULL"
            ),
        },
    }
    h["review_queue_dashboard_vs_badge"] = {
        "note": "Dashboard counts needs_review only; the nav badge ORs both flags",
        "value": {
            "needs_review_only": _one(
                conn, "SELECT COUNT(*) FROM documents WHERE needs_review IS TRUE"
            ),
            "either_flag": _one(
                conn,
                "SELECT COUNT(*) FROM documents "
                "WHERE needs_review IS TRUE OR needs_date_review IS TRUE",
            ),
        },
    }
    h["incomplete_tiles_true_counts"] = {
        "note": "The dashboard caps these at 100 via LIMIT; these are the real figures",
        "value": {
            "missing_text": _one(
                conn,
                "SELECT COUNT(*) FROM documents "
                "WHERE status IN ('COMPLETED','FAILED') "
                "AND (extracted_text IS NULL OR extracted_text = '')",
            ),
            "missing_keywords": _one(
                conn,
                "SELECT COUNT(*) FROM documents "
                "WHERE status IN ('COMPLETED','FAILED') AND keywords IS NULL",
            ),
            "missing_embeddings": _one(
                conn,
                "SELECT COUNT(*) FROM documents "
                "WHERE status IN ('COMPLETED','FAILED') AND search_vector IS NULL",
            ),
        },
    }
    h["duplicate_content_hashes"] = {
        "note": "Same file ingested more than once",
        "value": _one(
            conn,
            "SELECT COUNT(*) FROM (SELECT content_hash FROM documents "
            "WHERE content_hash IS NOT NULL GROUP BY content_hash "
            "HAVING COUNT(*) > 1) d",
        ),
    }
    h["ingest_source_split"] = {
        "note": "Unused dimension: cron ingest vs manual upload",
        "value": {
            "dropbox": _one(
                conn, "SELECT COUNT(*) FROM documents WHERE dropbox_file_id IS NOT NULL"
            ),
            "manual": _one(
                conn, "SELECT COUNT(*) FROM documents WHERE dropbox_file_id IS NULL"
            ),
        },
    }
    # The remaining checks read tables other than documents. A database that
    # has not run every migration may not have them, and a missing side table
    # should degrade one line of the report rather than abort the whole run.
    if present.get("taxonomy_terms"):
        rows = _fetch(
            conn,
            "SELECT primary_category, COUNT(*) FROM taxonomy_terms "
            "GROUP BY primary_category ORDER BY primary_category",
        )
        h["taxonomy_category_casing"] = {
            "note": "Catalog hazard: 'Demographic' vs 'demographic' as separate categories",
            "value": [{"category": r[0], "terms": r[1]} for r in rows],
        }

    if present.get("document_taxonomy_map"):
        h["taxonomy_two_sources_agree"] = {
            "note": "document_taxonomy_map rows vs documents carrying JSONB mappings",
            "value": {
                "join_table_documents": _one(
                    conn,
                    "SELECT COUNT(DISTINCT document_id) FROM document_taxonomy_map",
                ),
                "jsonb_mapping_documents": _one(
                    conn,
                    "SELECT COUNT(*) FROM documents "
                    "WHERE jsonb_array_length("
                    "  COALESCE(keywords -> 'keyword_mappings', '[]'::jsonb)) > 0",
                ),
            },
        }

    if present.get("search_queries"):
        h["search_sentinel_and_nulls"] = {
            "note": "'(filter only)' pollutes top-terms; user_id is never written",
            "value": {
                "filter_only_rows": _one(
                    conn,
                    "SELECT COUNT(*) FROM search_queries "
                    "WHERE query = '(filter only)'",
                ),
                "user_id_populated": _one(
                    conn,
                    "SELECT COUNT(*) FROM search_queries WHERE user_id IS NOT NULL",
                ),
                "result_count_null": _one(
                    conn,
                    "SELECT COUNT(*) FROM search_queries WHERE result_count IS NULL",
                ),
                "zero_result": _one(
                    conn, "SELECT COUNT(*) FROM search_queries WHERE result_count = 0"
                ),
            },
        }
    h["updated_at_null"] = {
        "note": "onupdate-only column: NULL until a row is first modified",
        "value": _one(conn, "SELECT COUNT(*) FROM documents WHERE updated_at IS NULL"),
    }
    return h


def profile_timings(conn):
    """
    Distribution of the one well-built timing pair, plus queue wait.

    Both are reported as percentiles rather than averages: the catalog notes a
    1800s soft timeout truncates the tail, which an average hides.
    """
    rows = _fetch(
        conn,
        """
        SELECT
            COUNT(*)                                                       AS n,
            ROUND(MIN(EXTRACT(EPOCH FROM (processed_at - processing_started_at)))::numeric, 1) AS min_s,
            ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (
                ORDER BY EXTRACT(EPOCH FROM (processed_at - processing_started_at)))::numeric, 1) AS p50_s,
            ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (
                ORDER BY EXTRACT(EPOCH FROM (processed_at - processing_started_at)))::numeric, 1) AS p95_s,
            ROUND(MAX(EXTRACT(EPOCH FROM (processed_at - processing_started_at)))::numeric, 1) AS max_s
        FROM documents
        WHERE status = 'COMPLETED'
          AND processed_at IS NOT NULL
          AND processing_started_at IS NOT NULL
        """,
    )
    worker = rows[0]._asdict() if rows else {}

    rows = _fetch(
        conn,
        """
        SELECT
            COUNT(*)                                                       AS n,
            ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (
                ORDER BY EXTRACT(EPOCH FROM (processing_started_at - created_at)))::numeric, 1) AS p50_s,
            ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (
                ORDER BY EXTRACT(EPOCH FROM (processing_started_at - created_at)))::numeric, 1) AS p95_s
        FROM documents
        WHERE processing_started_at IS NOT NULL AND created_at IS NOT NULL
        """,
    )
    queue_wait = rows[0]._asdict() if rows else {}

    legacy = _one(
        conn,
        "SELECT COUNT(*) FROM documents "
        "WHERE status = 'COMPLETED' AND processing_started_at IS NULL",
    )

    return {
        "worker_duration_seconds": worker,
        "queue_wait_seconds": queue_wait,
        "completed_without_start_time": {
            "note": "Excluded from worker duration entirely — no created_at fallback exists",
            "value": legacy,
        },
    }


def profile_cost(conn):
    """Token spend from file_metadata.processing_cost — the largest untapped measure."""
    rows = _fetch(
        conn,
        """
        SELECT
            COUNT(*)                                                      AS docs_with_cost,
            SUM((file_metadata -> 'processing_cost' ->> 'input_tokens')::bigint)  AS input_tokens,
            SUM((file_metadata -> 'processing_cost' ->> 'output_tokens')::bigint) AS output_tokens
        FROM documents
        WHERE file_metadata -> 'processing_cost' ->> 'input_tokens' ~ '^[0-9]+$'
        """,
    )
    totals = rows[0]._asdict() if rows else {}

    by_provider = [
        {"provider": r[0], "docs": r[1], "input_tokens": r[2], "output_tokens": r[3]}
        for r in _fetch(
            conn,
            """
            SELECT
                file_metadata -> 'processing_cost' ->> 'provider'              AS provider,
                COUNT(*)                                                       AS docs,
                SUM((file_metadata -> 'processing_cost' ->> 'input_tokens')::bigint),
                SUM((file_metadata -> 'processing_cost' ->> 'output_tokens')::bigint)
            FROM documents
            WHERE file_metadata -> 'processing_cost' ->> 'input_tokens' ~ '^[0-9]+$'
            GROUP BY 1 ORDER BY docs DESC
            """,
        )
    ]

    return {"totals": totals, "by_provider": by_provider}


def profile_pipeline_funnel(conn, total):
    """
    Stage-by-stage corpus coverage — the honest replacement for the four
    'Missing X' tiles, since each stage is a superset of the next.
    """
    stages = [
        ("uploaded", "1=1"),
        ("has extracted_text", "extracted_text IS NOT NULL AND extracted_text <> ''"),
        ("has ai_analysis", "ai_analysis IS NOT NULL"),
        ("has real summary",
         "ai_analysis ->> 'summary' IS NOT NULL "
         "AND ai_analysis ->> 'summary' <> '' "
         "AND ai_analysis ->> 'summary' NOT ILIKE '%no summary available%'"),
        ("has keyword mappings",
         "jsonb_array_length(COALESCE(keywords -> 'keyword_mappings', '[]'::jsonb)) > 0"),
        ("has embedding", "search_vector IS NOT NULL"),
        ("has client_canonical", "client_canonical IS NOT NULL AND client_canonical <> ''"),
        ("has state", "state IS NOT NULL AND TRIM(state) <> ''"),
        ("has date_created", "date_created IS NOT NULL"),
    ]
    out = []
    for label, predicate in stages:
        n = _one(conn, f"SELECT COUNT(*) FROM documents WHERE {predicate}")
        out.append(
            {
                "stage": label,
                "documents": n,
                "pct_of_corpus": round(n / total * 100, 2) if total else 0.0,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _bar(pct, width=24):
    filled = int(round(pct / 100 * width))
    return "#" * filled + "." * (width - filled)


def render_text(p):
    L = []
    add = L.append

    add("=" * 78)
    add("DATA PROFILE — pc-simple document catalog")
    add(f"generated {p['generated_at']}")
    add("=" * 78)

    add("")
    add("TABLE ROW COUNTS")
    add("-" * 78)
    for table, n in p["tables"].items():
        if n is None:
            add(f"  {table:<26} (table not present in this database)")
        else:
            add(f"  {table:<26} {n:>12,}")

    total = p["documents_total"]
    add("")
    add(f"DOCUMENTS COLUMN POPULATION  (n = {total:,})")
    add("-" * 78)
    for group, entries in p["documents_columns"].items():
        add("")
        add(f"  [{group}]")
        for e in entries:
            if not e["present_in_schema"]:
                add(f"    {e['column']:<26} -- not in deployed schema --")
                continue
            add(
                f"    {e['column']:<26} {e['pct']:>6.2f}%  {_bar(e['pct'])}  "
                f"{e['filled']:,}"
            )

    add("")
    add("PIPELINE FUNNEL")
    add("-" * 78)
    for s in p["pipeline_funnel"]:
        add(
            f"  {s['stage']:<26} {s['pct_of_corpus']:>6.2f}%  "
            f"{_bar(s['pct_of_corpus'])}  {s['documents']:,}"
        )

    add("")
    add("VALUE DOMAINS")
    add("-" * 78)
    for col, values in p["value_domains"].items():
        if values is None:
            add(f"  {col}: -- not in deployed schema --")
            continue
        add(f"  {col}:")
        for v in values:
            add(f"    {str(v['value']):<28} {v['count']:>12,}")

    add("")
    add("JSONB KEY COVERAGE")
    add("-" * 78)
    for column, keys in p["jsonb_keys"].items():
        add(f"  {column}:")
        for key, stat in keys.items():
            if isinstance(stat, dict) and "pct" in stat:
                add(f"    {key:<24} {stat['pct']:>6.2f}%  {stat['present']:,}")
            else:
                add(f"    {key:<24} {stat:,}" if stat is not None else f"    {key:<24} -")

    add("")
    add("TIMINGS")
    add("-" * 78)
    add(f"  worker duration: {p['timings']['worker_duration_seconds']}")
    add(f"  queue wait:      {p['timings']['queue_wait_seconds']}")
    cw = p["timings"]["completed_without_start_time"]
    add(f"  completed w/o start time: {cw['value']:,}  ({cw['note']})")

    add("")
    add("TOKEN COST")
    add("-" * 78)
    add(f"  totals: {p['cost']['totals']}")
    for row in p["cost"]["by_provider"]:
        add(f"    {row}")

    add("")
    add("HAZARD CHECKS")
    add("-" * 78)
    for name, h in p["hazards"].items():
        add(f"  {name}")
        add(f"    -> {h['value']}")
        add(f"       ({h['note']})")

    add("")
    add("=" * 78)
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    ap.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL"),
        help="defaults to $DATABASE_URL",
    )
    args = ap.parse_args()

    if not args.database_url:
        sys.exit(
            "No database URL. Set DATABASE_URL or pass --database-url.\n"
            "This script is read-only; point it at production."
        )

    url = args.database_url
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    engine = create_engine(url, pool_pre_ping=True)

    with engine.connect() as conn:
        # Belt and braces: even a bug in this script cannot write.
        conn.execute(text("SET TRANSACTION READ ONLY"))

        tables = profile_tables(conn)
        total = tables.get("documents") or 0
        if not total:
            sys.exit("documents table is empty or absent — nothing to profile.")

        profile = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "documents_total": total,
            "tables": tables,
            "documents_columns": profile_documents_columns(conn, total),
            "pipeline_funnel": profile_pipeline_funnel(conn, total),
            "value_domains": profile_value_domains(conn),
            "jsonb_keys": profile_jsonb_keys(conn, total),
            "timings": profile_timings(conn),
            "cost": profile_cost(conn),
            "hazards": profile_hazards(conn, tables),
        }

    if args.json:
        print(json.dumps(profile, indent=2, default=str))
    else:
        print(render_text(profile))


if __name__ == "__main__":
    main()
