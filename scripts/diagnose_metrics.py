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

It times each collector too, so a slow dashboard can be attributed to a
specific zone rather than guessed at.

Read-only: every collector issues SELECTs only.

Usage, from a Render shell on the web service:

    python -m scripts.diagnose_metrics
    python -m scripts.diagnose_metrics --predicates

``--predicates`` additionally times each funnel predicate on its own, which
attributes a slow funnel to a specific column rather than to the funnel as a
whole.
"""

import os
import sys
import time
import traceback

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

JSON_COLUMNS = ("ai_analysis", "keywords", "file_metadata", "embedding_provenance")


def time_predicates(engine):
    """
    Time each stage predicate on its own.

    The funnel evaluates all of them in one scan, so a slow funnel says
    nothing about *which* predicate is slow. Running each as a bare COUNT
    attributes the cost — and on a ``json`` column the JSON-touching ones
    dominate, because every operator re-parses the whole document per row.
    """
    from sqlalchemy import func, select

    from models.document import Document
    from services.metrics import jsonb, stages

    print()
    print("=" * 74)
    print("PER-PREDICATE COST")
    print("=" * 74)

    with Session(engine) as session:
        jsonb.configure(session)

    rows = []
    for st in (*stages.FUNNEL_STAGES, *stages.COVERAGE_STAGES):
        if st.is_root:
            continue
        with Session(engine) as session:
            stmt = select(func.count()).select_from(Document).where(st.predicate())
            started = time.perf_counter()
            try:
                n = session.execute(stmt).scalar()
                elapsed = time.perf_counter() - started
                rows.append((elapsed, st.key, n))
            except Exception as e:
                print(f"  FAIL {st.key}: {e}")

    if not rows:
        return
    total = sum(t for t, _, _ in rows)
    for elapsed, key, n in sorted(rows, reverse=True):
        share = elapsed / total * 100 if total else 0
        bar = "#" * int(round(share / 2))
        print(f"  {key:20} {elapsed * 1000:8.0f} ms  {share:5.1f}%  {n:>8,} rows  {bar}")
    print(f"\n  summed individually: {total:.1f}s")
    print("  (the funnel runs these in one scan, so its total is less than this)")


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

    # Detect the column types before running anything, exactly as the API
    # handlers do. Without this the collectors run in the undetected state,
    # which is a valid-but-slower code path and not what production executes —
    # so the timings would not describe the real thing.
    from services.metrics import jsonb

    with Session(engine) as session:
        jsonb.configure(session)

    failures = 0
    timings = []
    for label, cls in collectors:
        # A fresh session per collector: one failed statement aborts a
        # Postgres transaction, so a shared session would report every
        # subsequent collector as broken too. It also means each collector
        # pays its own corpus-total query rather than sharing the per-session
        # memo, so these timings are a slight over-estimate of live cost.
        session = Session(engine)
        started = time.perf_counter()
        try:
            group = cls(session).collect()
            elapsed = time.perf_counter() - started
            timings.append((elapsed, label))
            n = len(group.metrics)
            print(f"  OK   {label}  {elapsed * 1000:8.0f} ms  {n} metrics")
        except Exception:
            elapsed = time.perf_counter() - started
            failures += 1
            print(f"  FAIL {label}  {elapsed * 1000:8.0f} ms")
            for line in traceback.format_exc().splitlines():
                print(f"       {line}")
        finally:
            session.close()

    if timings:
        print()
        print("=" * 74)
        print("SLOWEST FIRST")
        print("=" * 74)
        total = sum(t for t, _ in timings)
        for elapsed, label in sorted(timings, reverse=True):
            share = elapsed / total * 100 if total else 0
            bar = "#" * int(round(share / 2))
            print(f"  {label}  {elapsed * 1000:8.0f} ms  {share:5.1f}%  {bar}")
        print(f"\n  total across collectors: {total:.1f}s")
        print()
        print("  The dashboard issues three requests: /metrics/now runs the")
        print("  'now' collector only; /metrics/pipeline runs pipeline +")
        print("  reliability + cost + activity; /metrics/corpus runs corpus +")
        print("  quality. The latter two are cached for 60s, so only the first")
        print("  load after a cache miss pays these costs.")

    if "--predicates" in sys.argv:
        time_predicates(engine)

    print()
    print("=" * 74)
    print(f"{len(collectors) - failures} of {len(collectors)} collectors OK")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
