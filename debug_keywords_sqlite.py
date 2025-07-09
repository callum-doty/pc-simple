#!/usr/bin/env python3
"""
Debug script to examine keyword extraction and storage using SQLite directly
"""

import asyncio
import json
import sqlite3
import os
from pathlib import Path


async def debug_keywords_sqlite():
    """Debug keyword extraction for documents using SQLite directly"""

    # Look for SQLite database files
    db_files = []
    for db_name in ["documents.db", "dev_documents.db", "test_documents.db"]:
        if os.path.exists(db_name):
            db_files.append(db_name)

    if not db_files:
        print("No SQLite database files found in current directory")
        print("Looking for database files...")
        for file in os.listdir("."):
            if file.endswith(".db"):
                db_files.append(file)

    if not db_files:
        print("No database files found!")
        return

    print(f"Found database files: {db_files}")

    # Use the first database file found
    db_path = db_files[0]
    print(f"Using database: {db_path}")

    try:
        # Connect to SQLite database
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row  # Enable column access by name
        cursor = conn.cursor()

        # Check if documents table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='documents';"
        )
        if not cursor.fetchone():
            print("Documents table not found in database")
            return

        # Get all documents
        cursor.execute("SELECT * FROM documents ORDER BY created_at DESC")
        documents = cursor.fetchall()

        print(f"Found {len(documents)} documents in database:")
        print("=" * 80)

        for doc in documents:
            print(f"\nDocument ID: {doc['id']}")
            print(f"Filename: {doc['filename']}")
            print(f"Status: {doc['status']}")
            print(f"Created: {doc['created_at']}")
            print(f"File Path: {doc['file_path']}")

            # Check extracted text
            if doc["extracted_text"]:
                print(
                    f"Extracted Text (first 200 chars): {doc['extracted_text'][:200]}..."
                )
            else:
                print("Extracted Text: None")

            # Check AI analysis
            if doc["ai_analysis"]:
                try:
                    ai_analysis = (
                        json.loads(doc["ai_analysis"])
                        if isinstance(doc["ai_analysis"], str)
                        else doc["ai_analysis"]
                    )
                    print(
                        f"AI Analysis Keys: {list(ai_analysis.keys()) if isinstance(ai_analysis, dict) else 'Not a dict'}"
                    )
                    if isinstance(ai_analysis, dict):
                        print("AI Analysis Structure:")
                        analysis_str = json.dumps(ai_analysis, indent=2)
                        print(
                            analysis_str[:1000] + "..."
                            if len(analysis_str) > 1000
                            else analysis_str
                        )
                except Exception as e:
                    print(f"Error parsing AI analysis: {e}")
                    print(f"Raw AI Analysis: {doc['ai_analysis']}")
            else:
                print("AI Analysis: None")

            # Check keywords
            if doc["keywords"]:
                try:
                    keywords = (
                        json.loads(doc["keywords"])
                        if isinstance(doc["keywords"], str)
                        else doc["keywords"]
                    )
                    print(f"Keywords JSON: {json.dumps(keywords, indent=2)}")

                    # Extract keywords and categories
                    if isinstance(keywords, dict):
                        keyword_list = keywords.get("keywords", [])
                        categories_list = keywords.get("categories", [])
                        print(f"Keywords List: {keyword_list}")
                        print(f"Categories List: {categories_list}")
                    else:
                        print(f"Keywords (raw): {keywords}")
                except Exception as e:
                    print(f"Error parsing keywords: {e}")
                    print(f"Raw Keywords: {doc['keywords']}")
            else:
                print("Keywords: None")

            # Check metadata
            if doc["file_metadata"]:
                try:
                    metadata = (
                        json.loads(doc["file_metadata"])
                        if isinstance(doc["file_metadata"], str)
                        else doc["file_metadata"]
                    )
                    print(f"File Metadata: {json.dumps(metadata, indent=2)}")
                except Exception as e:
                    print(f"Error parsing metadata: {e}")
                    print(f"Raw Metadata: {doc['file_metadata']}")
            else:
                print("File Metadata: None")

            print("-" * 80)

        # Look for Hinman document specifically
        cursor.execute(
            "SELECT * FROM documents WHERE filename LIKE '%hinman%' OR filename LIKE '%press%' ORDER BY created_at DESC"
        )
        hinman_docs = cursor.fetchall()

        if hinman_docs:
            print(f"\n\nFOUND {len(hinman_docs)} HINMAN/PRESS DOCUMENTS:")
            for doc in hinman_docs:
                print(f"\nDocument: {doc['filename']}")
                print(f"Status: {doc['status']}")
                print(f"File Path: {doc['file_path']}")

                # Show extracted text
                if doc["extracted_text"]:
                    print(f"Extracted Text: {doc['extracted_text'][:500]}...")

                # Show AI analysis in detail
                if doc["ai_analysis"]:
                    try:
                        ai_analysis = (
                            json.loads(doc["ai_analysis"])
                            if isinstance(doc["ai_analysis"], str)
                            else doc["ai_analysis"]
                        )
                        print("AI Analysis:")
                        print(json.dumps(ai_analysis, indent=2))
                    except Exception as e:
                        print(f"Error parsing AI analysis: {e}")

                # Show keywords in detail
                if doc["keywords"]:
                    try:
                        keywords = (
                            json.loads(doc["keywords"])
                            if isinstance(doc["keywords"], str)
                            else doc["keywords"]
                        )
                        print("Keywords:")
                        print(json.dumps(keywords, indent=2))
                    except Exception as e:
                        print(f"Error parsing keywords: {e}")
                else:
                    print("No keywords found!")
        else:
            print("\nNo Hinman/Press documents found")

        # Show table schema
        print("\n\nDOCUMENTS TABLE SCHEMA:")
        cursor.execute("PRAGMA table_info(documents)")
        columns = cursor.fetchall()
        for col in columns:
            print(f"  {col['name']}: {col['type']} (nullable: {not col['notnull']})")

    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()

    finally:
        if "conn" in locals():
            conn.close()


if __name__ == "__main__":
    asyncio.run(debug_keywords_sqlite())
