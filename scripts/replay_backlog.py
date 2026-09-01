"""
Phase 4 staged replay — audit and requeue the abandoned-document backlog.

Read-only by default. Both write modes are opt-in flags, because marking
hundreds of documents FAILED and spending real LLM budget are not things a
script should do because someone ran it to see what it said.

    python -m scripts.replay_backlog                      # audit only
    python -m scripts.replay_backlog --mark-missing       # + flag dead files
    python -m scripts.replay_backlog --replay             # + requeue survivors

Cohorts (see the Phase 4 runbook):

    fossils   PROCESSING created before --since — the pre-2026-05 backlog
    modern    PROCESSING created on or after --since — died under the current pipeline
    withtext  PROCESSING that already has extracted_text — investigate first
    queued    QUEUED — dispatched but never picked up

`--cohort` defaults to `fossils` so the bulk replay cannot accidentally sweep
up the cohorts the runbook wants handled individually.
"""

import argparse
import datetime as dt
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import or_

from config import get_settings
from database import SessionLocal
from models.document import Document, DocumentStatus
from services.document_service import DocumentService
from services.storage_service import StorageService

settings = get_settings()

TERMINAL = (DocumentStatus.COMPLETED, DocumentStatus.FAILED)
MISSING_FILE_ERROR = "Source file missing from storage"

# Boundary between the historical backlog and documents abandoned under the
# current pipeline. Cohorts are split on created_at rather than
# processing_started_at: that column was the natural discriminator, but a
# blanket `UPDATE ... SET processing_started_at = NULL` nulled it across every
# PROCESSING row. created_at survives because SQLAlchemy's onupdate is applied
# client-side and a raw SQL UPDATE never triggers it.
#
# The fossil cohort was created between 2025-08-05 and 2026-05-20 and stopped
# accumulating there; anything PROCESSING and newer died under current code.
DEFAULT_SINCE = "2026-06-01"

# Cohorts at or below this size get a per-document listing. The small cohorts
# are the ones the runbook handles individually, so their ids matter: `modern`
# is defined purely by date now, and any document that fails after deploy joins
# it — record the ids while the cohort is still just the historical member(s).
DETAIL_LIMIT = 20


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def file_exists(storage: StorageService, file_path: str) -> bool:
    """
    Does the object the worker will actually read exist?

    Deliberately not StorageService.check_file_exists: that method is async, and
    its local branch checks the raw file_path while _get_file_local_sync reads
    from `storage_path / basename`. Checking a different path than the read path
    would report files as missing that the worker can open, and vice versa. This
    mirrors the read path for each storage type instead.
    """
    if not file_path:
        return False

    if storage.storage_type == "s3":
        from botocore.exceptions import ClientError

        try:
            storage.s3_client.head_object(Bucket=settings.s3_bucket, Key=file_path)
            return True
        except ClientError:
            return False

    return os.path.exists(Path(storage.storage_path) / Path(file_path).name)


# ---------------------------------------------------------------------------
# Cohorts
# ---------------------------------------------------------------------------

def select_cohort(db, cohort: str, since: dt.date) -> List[Document]:
    q = db.query(Document)
    if cohort == "fossils":
        return (
            q.filter(
                Document.status == DocumentStatus.PROCESSING,
                Document.created_at < since,
                Document.extracted_text.is_(None),
            )
            .order_by(Document.created_at)
            .all()
        )
    if cohort == "modern":
        return q.filter(
            Document.status == DocumentStatus.PROCESSING,
            Document.created_at >= since,
        ).all()
    if cohort == "withtext":
        return q.filter(
            Document.status == DocumentStatus.PROCESSING,
            Document.extracted_text.isnot(None),
        ).all()
    if cohort == "queued":
        return q.filter(Document.status == DocumentStatus.QUEUED).all()
    raise ValueError(f"unknown cohort: {cohort}")


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def _page_count(doc: Document) -> Optional[int]:
    """Page count from wherever the pipeline happened to record it."""
    for blob in (doc.ai_analysis, doc.file_metadata):
        if isinstance(blob, dict) and blob.get("page_count"):
            return blob["page_count"]
    return None


def estimate_pages(db, docs: List[Document]) -> Optional[float]:
    """
    Extrapolate total pages from bytes, calibrated on documents that finished.

    Fossils never completed, so none of them recorded a page count; file_size is
    the only signal they all carry. Returns None when there is no calibration
    data rather than inventing a number.

    Note this is the only cost signal available: FIX-002's token accounting is
    written through Document.set_metadata, which mutates the JSON blob in place
    and so only persists when file_metadata was previously NULL. Real token
    counts are therefore too sparse to extrapolate from.
    """
    completed = (
        db.query(Document)
        .filter(
            Document.status == DocumentStatus.COMPLETED,
            Document.file_size.isnot(None),
            Document.file_size > 0,
        )
        .limit(2000)
        .all()
    )

    pairs = [(d.file_size, _page_count(d)) for d in completed]
    pairs = [(size, pages) for size, pages in pairs if pages]
    if not pairs:
        return None

    pages_per_byte = sum(p for _, p in pairs) / sum(s for s, _ in pairs)
    backlog_bytes = sum(d.file_size or 0 for d in docs)
    return backlog_bytes * pages_per_byte


def audit(db, storage: StorageService, cohort: str, since: dt.date):
    docs = select_cohort(db, cohort, since)
    print(f"\ncohort '{cohort}': {len(docs)} document(s)")
    if not docs:
        return [], []

    print("checking storage (this makes one HEAD request per document)...")
    present, missing = [], []
    for i, doc in enumerate(docs, 1):
        (present if file_exists(storage, doc.file_path) else missing).append(doc)
        if i % 50 == 0:
            print(f"  ...{i}/{len(docs)}")

    total_mb = sum(d.file_size or 0 for d in present) / (1024 * 1024)
    print(f"\n  files present : {len(present)}")
    print(f"  files missing : {len(missing)}")
    print(f"  replay size   : {total_mb:,.0f} MB")

    pages = estimate_pages(db, present)
    if pages:
        print(f"  est. pages    : ~{pages:,.0f}  (one LLM call each, no checkpoint resume)")
        print(f"  >> multiply by your per-page rate before running --replay")
    else:
        print("  est. pages    : unknown (no completed document records a page count)")

    if missing:
        print(f"\n  missing file_path examples: {[d.file_path for d in missing[:3]]}")

    if 0 < len(docs) <= DETAIL_LIMIT:
        print(f"\n  {'id':>7}  {'created':10}  {'MB':>6}  text  vec  file")
        for doc in docs:
            size_mb = (doc.file_size or 0) / (1024 * 1024)
            # has_vector on a document that never completed means it reached
            # extract_document_features_task, which only runs after COMPLETED —
            # i.e. it finished once and was flipped back. Different bug from the
            # rest of the cohort; investigate before replaying over the evidence.
            print(
                f"  {doc.id:>7}  {str(doc.created_at.date()):10}  {size_mb:>6.1f}"
                f"  {'yes' if doc.extracted_text else ' no':>4}"
                f"  {'yes' if doc.search_vector is not None else ' no':>3}"
                f"  {'ok' if doc in present else 'MISSING'}"
            )

    return present, missing


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def mark_missing(db, missing: List[Document]) -> int:
    for doc in missing:
        doc.status = DocumentStatus.FAILED
        doc.processing_error = MISSING_FILE_ERROR
        doc.processing_started_at = None
        doc.processing_heartbeat_at = None
    db.commit()
    print(f"marked {len(missing)} document(s) FAILED ({MISSING_FILE_ERROR})")
    return len(missing)


def requeue_and_dispatch(db, docs: List[Document]) -> int:
    """
    Requeue via update_document_status_sync, which clears the previous run's
    processing_started_at and heartbeat so this run is measured on its own.

    Not reset_document_for_reprocessing: that is an async method on
    DocumentService, and it also clears extracted_text, ai_analysis, keywords,
    embeddings and taxonomy links — destructive here, and pointless for a
    document that never produced any of them.
    """
    from worker import process_document_task

    service = DocumentService(db)
    dispatched = 0
    for doc in docs:
        service.update_document_status_sync(doc.id, DocumentStatus.QUEUED)
        try:
            process_document_task.delay(doc.id)
            dispatched += 1
        except Exception as e:
            # Left QUEUED — the recovery daemon's undispatched sweep retries it
            # once beat is running.
            print(f"  ! dispatch failed for {doc.id}: {e}")
    return dispatched


def await_batch(db, doc_ids: List[int], timeout: int) -> Counter:
    """Poll until every document reaches a terminal state, or timeout."""
    deadline = time.time() + timeout
    while True:
        db.expire_all()
        rows = db.query(Document.status).filter(Document.id.in_(doc_ids)).all()
        counts = Counter(r[0] for r in rows)
        done = sum(counts[s] for s in TERMINAL)
        # Pad to overwrite the previous, possibly longer, line: \r only moves the
        # cursor, it does not clear what is already there.
        print(f"  {done}/{len(doc_ids)} terminal  {dict(counts)}".ljust(78), end="\r", flush=True)
        if done == len(doc_ids) or time.time() > deadline:
            print()
            return counts
        time.sleep(15)


def replay(db, docs: List[Document], batch_size: int, timeout: int):
    batches = [docs[i:i + batch_size] for i in range(0, len(docs), batch_size)]
    print(f"\nreplaying {len(docs)} document(s) in {len(batches)} batch(es) of {batch_size}")

    for n, batch in enumerate(batches, 1):
        ids = [d.id for d in batch]
        print(f"\n--- batch {n}/{len(batches)} ({len(ids)} docs) ---")
        print(f"  dispatched {requeue_and_dispatch(db, batch)}")

        counts = await_batch(db, ids, timeout)
        failed = counts[DocumentStatus.FAILED]
        rate = failed / len(ids) * 100
        print(f"  completed {counts[DocumentStatus.COMPLETED]}  failed {failed}  ({rate:.0f}% failure)")

        if failed:
            for doc in db.query(Document).filter(
                Document.id.in_(ids), Document.status == DocumentStatus.FAILED
            ).limit(3):
                print(f"    {doc.id}: {(doc.processing_error or '')[:120]}")

        if n < len(batches):
            if input("\ncontinue? [y/N] ").strip().lower() != "y":
                remaining = sum(len(b) for b in batches[n:])
                print(f"stopped. {remaining} document(s) left untouched in PROCESSING.")
                return


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cohort", default="fossils", choices=["fossils", "modern", "withtext", "queued"])
    p.add_argument("--since", default=DEFAULT_SINCE,
                   help=f"boundary between historical and current cohorts (default {DEFAULT_SINCE})")
    p.add_argument("--mark-missing", action="store_true", help="flag documents whose file is gone as FAILED")
    p.add_argument("--replay", action="store_true", help="requeue and dispatch survivors in batches")
    p.add_argument("--batch-size", type=int, default=25)
    p.add_argument("--batch-timeout", type=int, default=1800, help="seconds to wait for a batch to drain")
    args = p.parse_args()

    since = dt.date.fromisoformat(args.since)
    storage = StorageService()
    print(f"storage: {storage.storage_type}   broker: {settings.redis_url.split('@')[-1]}")

    with SessionLocal() as db:
        present, missing = audit(db, storage, args.cohort, since)

        if not (args.mark_missing or args.replay):
            print("\n(audit only — pass --mark-missing and/or --replay to make changes)")
            return

        if args.mark_missing and missing:
            if input(f"\nmark {len(missing)} document(s) FAILED? [y/N] ").strip().lower() == "y":
                mark_missing(db, missing)

        if args.replay and present:
            print(f"\nabout to replay {len(present)} document(s) — this spends LLM budget.")
            if input("proceed? [y/N] ").strip().lower() == "y":
                replay(db, present, args.batch_size, args.batch_timeout)


if __name__ == "__main__":
    main()
