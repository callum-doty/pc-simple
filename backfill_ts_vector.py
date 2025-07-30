import asyncio
import logging
from sqlalchemy import func
from database import SessionLocal, init_db
from models.document import Document

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def backfill_ts_vectors():
    """
    Backfills the ts_vector column for all existing documents.
    """
    await init_db()
    db = SessionLocal()
    try:
        documents = db.query(Document).all()
        logger.info(f"Found {len(documents)} documents to process.")

        for i, doc in enumerate(documents):
            search_parts = [doc.filename]
            if doc.extracted_text:
                search_parts.append(doc.extracted_text)

            if doc.ai_analysis:
                if doc.ai_analysis.get("summary"):
                    search_parts.append(doc.ai_analysis.get("summary"))
                if doc.ai_analysis.get("content_analysis"):
                    search_parts.append(doc.ai_analysis.get("content_analysis"))
                if doc.ai_analysis.get("title"):
                    search_parts.append(doc.ai_analysis.get("title"))

            if doc.keywords:
                if doc.keywords.get("keywords"):
                    search_parts.extend(doc.keywords.get("keywords", []))
                if doc.keywords.get("categories"):
                    search_parts.extend(doc.keywords.get("categories", []))

                keyword_mappings = doc.keywords.get("keyword_mappings", [])
                if keyword_mappings:
                    verbatim_terms = [
                        m.get("verbatim_term")
                        for m in keyword_mappings
                        if m.get("verbatim_term")
                    ]
                    search_parts.extend(verbatim_terms)

            # Reconstruct search_content
            search_content = " ".join(
                sorted(list(set(str(p) for p in search_parts if p)))
            )
            doc.search_content = search_content

            # Compute and set the ts_vector
            doc.ts_vector = func.to_tsvector("english", search_content)

            if (i + 1) % 100 == 0:
                logger.info(f"Processed {i + 1}/{len(documents)} documents...")
                db.commit()

        db.commit()
        logger.info("Successfully backfilled ts_vector for all documents.")
    except Exception as e:
        logger.error(f"An error occurred during backfill: {e}")
        db.rollback()
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(backfill_ts_vectors())
