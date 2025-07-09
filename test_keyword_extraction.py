#!/usr/bin/env python3
"""
Test script to specifically test keyword extraction for the Hinman document
"""

import asyncio
import json
import os

# Set environment variable to force SQLite usage
os.environ["DATABASE_URL"] = "sqlite:///./documents.db"

from services.ai_service import AIService
from database import init_db, SessionLocal
from models.document import Document


async def test_keyword_extraction():
    """Test keyword extraction specifically"""

    print("Testing Keyword Extraction for Hinman Document...")
    print("=" * 60)

    try:
        # Initialize database
        await init_db()
        db = SessionLocal()

        # Define the document details
        hinman_filename = "Hinman G02 Intro-PRESS.pdf"

        # Let's find the file in the project directory, starting from the simplified_app directory
        hinman_file_path = None
        start_path = os.path.join(os.path.dirname(__file__))
        for root, dirs, files in os.walk(start_path):
            if hinman_filename in files:
                hinman_file_path = os.path.join(root, hinman_filename)
                break

        if not hinman_file_path:
            print(f"'{hinman_filename}' not found in the project.")
            return

        # Check if the document already exists in the DB
        hinman_doc = (
            db.query(Document).filter(Document.filename == hinman_filename).first()
        )

        if not hinman_doc:
            print("Hinman document not found in the database, adding it...")
            # Add the document to the database
            file_size = os.path.getsize(hinman_file_path)

            hinman_doc = Document(
                filename=hinman_filename,
                file_path=hinman_file_path,
                file_size=file_size,
                status="uploaded",
            )
            db.add(hinman_doc)
            db.commit()
            db.refresh(hinman_doc)
            print("Document added to the database.")

        print(f"Found document: {hinman_doc.filename}")
        print(f"File path: {hinman_doc.file_path}")

        # Create AI service
        ai_service = AIService()
        print(f"AI Provider: {ai_service.ai_provider}")

        # Test different analysis types
        analysis_types = ["keywords", "unified", "modular"]

        for analysis_type in analysis_types:
            print(f"\n{'='*20} Testing {analysis_type.upper()} Analysis {'='*20}")

            try:
                result = await ai_service.analyze_document(
                    hinman_doc.file_path, hinman_doc.filename, analysis_type
                )

                print(f"\nAnalysis Type: {analysis_type}")
                print(f"AI Analysis Keys: {list(result.get('ai_analysis', {}).keys())}")

                # Show the full AI analysis
                ai_analysis = result.get("ai_analysis", {})
                if ai_analysis:
                    print(f"\nFull AI Analysis:")
                    print(json.dumps(ai_analysis, indent=2))

                # Extract keywords using the service's method
                keywords, categories = ai_service._extract_keywords_from_analysis(
                    ai_analysis
                )
                print(f"\nExtracted Keywords: {keywords}")
                print(f"Extracted Categories: {categories}")

                # Check for keyword mappings specifically
                if "keyword_mappings" in ai_analysis:
                    print(f"\nKeyword Mappings Found:")
                    for mapping in ai_analysis["keyword_mappings"]:
                        print(f"  - Verbatim: '{mapping.get('verbatim_term')}'")
                        print(
                            f"    Canonical: '{mapping.get('mapped_canonical_term')}'"
                        )
                        print(
                            f"    Category: '{mapping.get('mapped_primary_category')}'"
                        )

            except Exception as e:
                print(f"Error with {analysis_type} analysis: {e}")
                import traceback

                traceback.print_exc()

        # Test the taxonomy keyword prompt specifically
        print(f"\n{'='*20} Testing Taxonomy Keyword Prompt {'='*20}")

        try:
            from services.prompt_manager import PromptManager

            prompt_manager = PromptManager()

            # Get the taxonomy keyword prompt
            prompt_data = prompt_manager.get_taxonomy_keyword_prompt(
                hinman_doc.filename
            )

            print("Taxonomy Keyword Prompt:")
            print("-" * 40)
            print(
                prompt_data["user"][:1000] + "..."
                if len(prompt_data["user"]) > 1000
                else prompt_data["user"]
            )

        except Exception as e:
            print(f"Error getting taxonomy prompt: {e}")

        db.close()

    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(test_keyword_extraction())
