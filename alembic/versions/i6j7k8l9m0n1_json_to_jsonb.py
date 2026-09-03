"""Convert the documents JSON columns from json to jsonb

Revision ID: i6j7k8l9m0n1
Revises: h5i6j7k8l9m0
Create Date: 2026-09-03

models/document.py has always declared ai_analysis, keywords, file_metadata and
embedding_provenance as JSONB, and the squashed migration creates them that
way — but the deployed database has them as plain ``json``. services/
search_service.py has worked around it for a long time, casting
``documents.keywords::jsonb`` in four places with the comment "the keywords
column is json type".

Two costs came with that:

1. Correctness. ``jsonb_exists`` and ``jsonb_array_length`` have no ``json``
   overload, and ``COALESCE(json_value, '[]'::jsonb)`` cannot match types, so
   the dashboard's funnel and corpus endpoints returned 500s.

2. Speed. Every read that needed jsonb semantics cast the column per row, and
   ``json -> jsonb`` re-parses the whole document each time. A GIN index is
   also impossible on ``json``, so no JSON predicate could ever use one.

This settles the type so the declaration is true, the casts become redundant,
and the GIN index actually applies.

OPERATIONAL NOTE — read before deploying:

  ALTER COLUMN ... TYPE rewrites the entire table and holds an ACCESS
  EXCLUSIVE lock for the duration. Nothing can read or write ``documents``
  while it runs, including the currently-serving instance, since Render runs
  ``alembic upgrade head`` in buildCommand before the new instance starts.

  On a corpus of a few tens of thousands of rows with sizeable extracted_text
  this is minutes, not seconds — the rewrite copies every row including the
  TOASTed columns.

  ``lock_timeout`` below makes the migration fail fast rather than queue
  behind a long-running query and block every other session while it waits.
  If it fails, retry during a quiet period.
"""

from alembic import op
from sqlalchemy import text

revision = "i6j7k8l9m0n1"
down_revision = "h5i6j7k8l9m0"
branch_labels = None
depends_on = None

#: Columns declared JSONB in the model. Converted only where the deployed type
#: actually differs, so re-running is free and a database already correct is
#: untouched.
JSON_COLUMNS = ("ai_analysis", "keywords", "file_metadata", "embedding_provenance")


def _current_type(conn, column: str):
    return conn.execute(
        text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'documents' "
            "AND column_name = :c"
        ),
        {"c": column},
    ).scalar()


def upgrade() -> None:
    conn = op.get_bind()

    # Fail fast instead of blocking the whole database behind a lock queue.
    conn.execute(text("SET lock_timeout = '30s'"))

    # The GIN index is dropped first and recreated after. Postgres would
    # rebuild it automatically, but doing it explicitly keeps the operator
    # class correct: jsonb_path_ops is roughly half the size of the default
    # and covers the containment queries this column is actually searched by.
    conn.execute(text("DROP INDEX IF EXISTS idx_documents_keywords"))

    for column in JSON_COLUMNS:
        current = _current_type(conn, column)
        if current is None:
            # Column absent on this database — nothing to convert.
            continue
        if current == "jsonb":
            continue
        conn.execute(
            text(
                f"ALTER TABLE documents "
                f"ALTER COLUMN {column} TYPE jsonb USING {column}::jsonb"
            )
        )

    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_documents_keywords "
            "ON documents USING gin (keywords jsonb_path_ops)"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("SET lock_timeout = '30s'"))
    conn.execute(text("DROP INDEX IF EXISTS idx_documents_keywords"))
    for column in JSON_COLUMNS:
        current = _current_type(conn, column)
        if current is None or current == "json":
            continue
        conn.execute(
            text(
                f"ALTER TABLE documents "
                f"ALTER COLUMN {column} TYPE json USING {column}::json"
            )
        )
    # No GIN index on the way back: json has no GIN operator class, which is
    # part of why this migration exists.
