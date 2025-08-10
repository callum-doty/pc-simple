import asyncio
import logging
from sqlalchemy.orm import Session
from database import SessionLocal, get_db
from models.document import Document
from services.document_service import DocumentService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def backfill_keyword_mappings():
    """
    One-time script to backfill the new `keyword_mappings` structure
    for existing documents. This ensures that all documents are searchable
    by canonical term.
    """
    db: Session = next(get_db())
    doc_service = DocumentService(db)

    try:
        logger.info("Starting backfill process for keyword mappings...")

        # Get all documents that might need backfilling
        documents_to_process = db.query(Document).all()

        if not documents_to_process:
            logger.info("No documents found to process.")
            return

        logger.info(f"Found {len(documents_to_process)} documents to check.")

        processed_count = 0
        for doc in documents_to_process:
            # Check if mappings are already present
            if doc.keywords and doc.keywords.get("keyword_mappings"):
                continue

            # Extract mappings from ai_analysis
            if doc.ai_analysis and "keyword_mappings" in doc.ai_analysis:
                keyword_mappings = doc.ai_analysis.get("keyword_mappings", [])

                if keyword_mappings:
                    # Update the document with the extracted mappings
                    await doc_service.update_document_content(
                        document_id=doc.id,
                        keyword_mappings=keyword_mappings,
                    )
                    processed_count += 1
                    logger.info(f"Backfilled mappings for document ID: {doc.id}")

        logger.info(
            f"Backfill process completed. {processed_count} documents were updated."
        )

    except Exception as e:
        logger.error(f"An error occurred during the backfill process: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(backfill_keyword_mappings())
