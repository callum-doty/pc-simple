#!/usr/bin/env python3
"""
Debug script to examine keyword extraction and storage
"""

import asyncio
import json
from database import SessionLocal, init_db
from models.document import Document
from services.ai_service import AIService
from services.document_service import DocumentService


async def debug_keywords():
    """Debug keyword extraction for documents"""

    # Initialize database
    await init_db()

    db = SessionLocal()

    try:
        # Get all documents
        documents = db.query(Document).all()

        print(f"Found {len(documents)} documents in database:")
        print("=" * 80)

        for doc in documents:
            print(f"\nDocument ID: {doc.id}")
            print(f"Filename: {doc.filename}")
            print(f"Status: {doc.status}")
            print(f"Created: {doc.created_at}")
            print(f"File Path: {doc.file_path}")

            # Check extracted text
            if doc.extracted_text:
                print(
                    f"Extracted Text (first 200 chars): {doc.extracted_text[:200]}..."
                )
            else:
                print("Extracted Text: None")

            # Check AI analysis
            if doc.ai_analysis:
                print(
                    f"AI Analysis Keys: {list(doc.ai_analysis.keys()) if isinstance(doc.ai_analysis, dict) else 'Not a dict'}"
                )
                if isinstance(doc.ai_analysis, dict):
                    print("AI Analysis Structure:")
                    print(
                        json.dumps(doc.ai_analysis, indent=2)[:1000] + "..."
                        if len(json.dumps(doc.ai_analysis, indent=2)) > 1000
                        else json.dumps(doc.ai_analysis, indent=2)
                    )
            else:
                print("AI Analysis: None")

            # Check keywords
            if doc.keywords:
                print(f"Keywords JSON: {json.dumps(doc.keywords, indent=2)}")
                print(f"Keywords List: {doc.get_keyword_list()}")
                print(f"Categories List: {doc.get_categories()}")
            else:
                print("Keywords: None")

            # Check metadata
            if doc.file_metadata:
                print(f"File Metadata: {json.dumps(doc.file_metadata, indent=2)}")
            else:
                print("File Metadata: None")

            print("-" * 80)

        # If we have a specific document that should have keywords, let's analyze it
        hinman_doc = None
        for doc in documents:
            if "hinman" in doc.filename.lower() or "press" in doc.filename.lower():
                hinman_doc = doc
                break

        if hinman_doc:
            print(f"\n\nFOCUSING ON HINMAN DOCUMENT:")
            print(f"Document: {hinman_doc.filename}")

            # Try to re-analyze this document
            ai_service = AIService()
            print(f"AI Provider: {ai_service.ai_provider}")

            if hinman_doc.status == "COMPLETED" and hinman_doc.file_path:
                print("Re-analyzing document...")
                try:
                    result = await ai_service.analyze_document(
                        hinman_doc.file_path,
                        hinman_doc.filename,
                        "keywords",  # Focus on keywords analysis
                    )

                    print("Fresh Analysis Result:")
                    print(json.dumps(result, indent=2))

                    # Extract keywords from fresh analysis
                    keywords, categories = ai_service._extract_keywords_from_analysis(
                        result.get("ai_analysis", {})
                    )
                    print(f"\nExtracted Keywords: {keywords}")
                    print(f"Extracted Categories: {categories}")

                except Exception as e:
                    print(f"Error re-analyzing: {e}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()

    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(debug_keywords())
