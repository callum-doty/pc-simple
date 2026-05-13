"""
Backfill embeddings for existing documents using the structured synthesis strategy.

Run with:
    python backfill_embeddings.py [--batch-size 50] [--dry-run]

Documents are processed in order of most recently accessed (updated_at DESC) so
hot documents improve first. Existing embeddings remain valid throughout the run;
the backfill is safe to interrupt and resume.
"""

import argparse
import logging
import sys
from sqlalchemy import or_

from database import get_db
from models.document import Document
from services.ai_service import AIService
from services.document_service import DocumentService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def run_backfill(batch_size: int = 50, dry_run: bool = False) -> None:
    db = next(get_db())
    try:
        ai_service = AIService(db)
        doc_service = DocumentService(db)

        # Select documents that have ai_analysis but are on an older embedding
        # version (or have never been versioned). Ordered by most recently
        # updated so hot documents improve first.
        query = (
            db.query(Document)
            .filter(Document.ai_analysis.isnot(None))
            .filter(
                or_(
                    Document.embedding_version.is_(None),
                    Document.embedding_version < AIService.EMBEDDING_VERSION,
                )
            )
            .order_by(Document.updated_at.desc().nullslast())
        )

        # Fetch IDs only — a separate per-document query is used inside the loop.
        # yield_per() opens a server-side cursor that is invalidated when
        # update_document_embeddings_sync commits, crashing after the first batch.
        doc_ids = [
            row.id for row in query.with_entities(Document.id).all()
        ]
        total = len(doc_ids)
        logger.info(
            f"Found {total} documents to re-embed "
            f"(target version: {AIService.EMBEDDING_VERSION}, "
            f"model: {AIService.EMBEDDING_MODEL})"
        )
        if dry_run:
            logger.info("Dry run — no changes will be written.")

        processed = skipped = failed = 0

        for doc_id in doc_ids:
            doc = db.query(Document).get(doc_id)
            if doc is None:
                skipped += 1
                continue

            embedding_text, provenance = AIService.build_embedding_text(
                doc.ai_analysis,
                filename=doc.filename,
                client_canonical=doc.client_canonical,
                client_confidence=doc.client_confidence,
                state=doc.state,
                state_confidence=doc.state_confidence,
            )
            if not provenance:
                logger.warning(f"doc {doc_id}: ai_analysis is null or empty, skipping")
                skipped += 1
                continue

            if dry_run:
                logger.info(f"doc {doc_id}: would embed → {embedding_text[:120]!r}")
                logger.info(f"doc {doc_id}: provenance fields → {list(provenance.keys())}")
                processed += 1
                continue

            embeddings = ai_service.generate_embeddings_sync(embedding_text)
            if not embeddings:
                logger.error(f"doc {doc_id}: embedding generation failed")
                failed += 1
                continue

            ok = doc_service.update_document_embeddings_sync(
                doc_id,
                embeddings,
                embedding_model=AIService.EMBEDDING_MODEL,
                embedding_version=AIService.EMBEDDING_VERSION,
                embedding_provenance=provenance,
            )
            if ok:
                processed += 1
                logger.info(
                    f"doc {doc_id} ({doc.filename[:60]}): re-embedded "
                    f"[{processed}/{total}]"
                )
            else:
                failed += 1
                logger.error(f"doc {doc_id}: DB update failed")

        logger.info(
            f"Backfill complete — processed: {processed}, "
            f"skipped: {skipped}, failed: {failed}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill document embeddings")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Unused — kept for backwards compatibility",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be embedded without writing to DB",
    )
    args = parser.parse_args()

    try:
        run_backfill(batch_size=args.batch_size, dry_run=args.dry_run)
    except KeyboardInterrupt:
        logger.info("Interrupted — progress is preserved, safe to re-run.")
        sys.exit(0)
