"""
One-time script to repopulate the document_taxonomy_map table for existing documents.
"""

import asyncio
import logging
from database import SessionLocal, init_db
from models.document import Document
from services.document_service import DocumentService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def repopulate_mappings():
    """
    Iterates through all existing documents and repopulates their taxonomy mappings.
    """
    await init_db()
    db = SessionLocal()
    document_service = DocumentService(db)

    try:
        logger.info("Starting taxonomy mapping repopulation...")
        documents = db.query(Document).all()
        total_docs = len(documents)
        logger.info(f"Found {total_docs} documents to process.")

        for i, doc in enumerate(documents):
            logger.info(f"Processing document {i + 1}/{total_docs} (ID: {doc.id})")
            if doc.keywords and "keyword_mappings" in doc.keywords:
                keyword_mappings = doc.keywords["keyword_mappings"]
                if keyword_mappings:
                    try:
                        document_service._update_document_taxonomy_mappings(
                            doc, keyword_mappings
                        )
                        db.commit()
                        logger.info(
                            f"Successfully updated mappings for document {doc.id}"
                        )
                    except Exception as e:
                        db.rollback()
                        logger.error(
                            f"Error updating mappings for document {doc.id}: {e}"
                        )
                else:
                    logger.info(
                        f"No keyword mappings found for document {doc.id}, skipping."
                    )
            else:
                logger.info(
                    f"No keywords or keyword_mappings field for document {doc.id}, skipping."
                )

        logger.info("Taxonomy mapping repopulation complete.")

    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(repopulate_mappings())
