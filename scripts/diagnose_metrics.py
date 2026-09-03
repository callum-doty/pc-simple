"""
Isolate which metrics collector fails, and why.

The dashboard endpoints deliberately return a generic message and log the
traceback, which is right for production and useless for debugging from the
outside. This runs each collector on its own and prints the real exception.

It also dumps the deployed column types for the JSON columns, because the two
faults this was written to chase are both schema-shaped:

  * ``ai_analysis`` / ``keywords`` / ``file_metadata`` declared JSONB in the
    model but created as plain ``json`` in the database, for which
    ``jsonb_exists`` and ``jsonb_array_length`` do not exist.
  * PostgreSQL older than 14, where the ``col['key']`` subscript syntax
    SQLAlchemy 2.0 emits for JSON columns is a syntax error.

Read-only: every collector issues SELECTs only.

Usage, from a Render shell on the web service:

    python -m scripts.diagnose_metrics
"""

import os
import sys
import traceback

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

JSON_COLUMNS = ("ai_analysis", "keywords", "file_metadata", "embedding_provenance")


def main():
    url = os.getenv("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set.")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    engine = create_engine(url, pool_pre_ping=True)

    print("=" * 74)
    print("SERVER")
    print("=" * 74)
    with engine.connect() as conn:
        version = conn.execute(text("SHOW server_version")).scalar()
        print(f"  postgres {version}")
        major = int(str(version).split(".")[0])
        if major < 14:
            print("  ^ below 14: col['key'] subscript syntax is unsupported here")

        print()
        print("=" * 74)
        print("JSON COLUMN TYPES")
        print("=" * 74)
        rows = conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='documents' "
                "AND column_name = ANY(:cols) ORDER BY column_name"
            ),
            {"cols": list(JSON_COLUMNS)},
        ).fetchall()
        for name, kind in rows:
            flag = "  <-- plain json, not jsonb" if kind == "json" else ""
            print(f"  {name:24} {kind}{flag}")

    print()
    print("=" * 74)
    print("COLLECTORS")
    print("=" * 74)

    # Imported here so a failure above still prints, and so an import error in
    # the metrics package is reported as such rather than killing the script.
    from services.metrics.activity import ActivityMetrics
    from services.metrics.corpus import CorpusMetrics
    from services.metrics.cost import CostMetrics
    from services.metrics.now import NowMetrics
    from services.metrics.pipeline import PipelineMetrics
    from services.metrics.quality import QualityMetrics
    from services.metrics.reliability import ReliabilityMetrics

    collectors = [
        ("now        (Zone 0)", NowMetrics),
        ("pipeline   (Zone 1)", PipelineMetrics),
        ("reliability(Zone 2)", ReliabilityMetrics),
        ("cost       (Zone 3)", CostMetrics),
        ("corpus     (Zone 4)", CorpusMetrics),
        ("quality    (Zone 5)", QualityMetrics),
        ("activity           ", ActivityMetrics),
    ]

    failures = 0
    for label, cls in collectors:
        # A fresh session per collector: one failed statement aborts a
        # Postgres transaction, so a shared session would report every
        # subsequent collector as broken too.
        session = Session(engine)
        try:
            group = cls(session).collect()
            n = len(group.metrics)
            series = ", ".join(group.series) if group.series else "—"
            print(f"  OK   {label}  {n} metrics | series: {series}")
        except Exception:
            failures += 1
            print(f"  FAIL {label}")
            for line in traceback.format_exc().splitlines():
                print(f"       {line}")
        finally:
            session.close()

    print()
    print("=" * 74)
    print(f"{len(collectors) - failures} of {len(collectors)} collectors OK")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
