#!/usr/bin/env python3
"""
Test script to directly test search functionality and keyword display
"""

import asyncio
import json
import os
import sys

# Set environment variable to force SQLite usage
os.environ["DATABASE_URL"] = "sqlite:///./documents.db"

from services.search_service import SearchService
from database import init_db


async def test_search_api():
    """Test the search API functionality"""

    print("Testing Search API functionality...")
    print("=" * 60)

    try:
        # Initialize database
        await init_db()

        # Create search service
        search_service = SearchService()

        # Test 1: Search for all documents
        print("\n1. Testing search for all documents:")
        results = await search_service.search(query="", page=1, per_page=10)

        print(f"Total documents found: {results['total_count']}")
        print(f"Documents returned: {len(results['documents'])}")

        # Show each document with keywords
        for i, doc in enumerate(results["documents"], 1):
            print(f"\n  Document {i}:")
            print(f"    ID: {doc['id']}")
            print(f"    Filename: {doc['filename']}")
            print(f"    Summary: {doc['summary']}")
            print(f"    Keywords: {doc['keywords']}")
            print(f"    Categories: {doc['categories']}")

        # Test 2: Search specifically for Hinman
        print("\n\n2. Testing search for 'Hinman':")
        hinman_results = await search_service.search(
            query="Hinman", page=1, per_page=10
        )

        print(f"Hinman documents found: {hinman_results['total_count']}")

        for i, doc in enumerate(hinman_results["documents"], 1):
            print(f"\n  Hinman Document {i}:")
            print(f"    ID: {doc['id']}")
            print(f"    Filename: {doc['filename']}")
            print(f"    Summary: {doc['summary']}")
            print(f"    Keywords: {doc['keywords']}")
            print(f"    Categories: {doc['categories']}")
            print(f"    Preview URL: {doc['preview_url']}")

        # Test 3: Search for Dave Hinman specifically
        print("\n\n3. Testing search for 'Dave Hinman':")
        dave_results = await search_service.search(
            query="Dave Hinman", page=1, per_page=10
        )

        print(f"Dave Hinman documents found: {dave_results['total_count']}")

        for i, doc in enumerate(dave_results["documents"], 1):
            print(f"\n  Dave Hinman Document {i}:")
            print(f"    ID: {doc['id']}")
            print(f"    Filename: {doc['filename']}")
            print(f"    Summary: {doc['summary']}")
            print(f"    Keywords: {doc['keywords']}")
            print(f"    Categories: {doc['categories']}")

        # Test 4: Check facets
        print("\n\n4. Testing facets:")
        facets = results.get("facets", {})
        print(f"Available categories: {len(facets.get('categories', []))}")

        for category in facets.get("categories", [])[:10]:  # Show first 10
            print(f"  - {category['name']}: {category['count']} documents")

        # Test 5: Search by category
        if facets.get("categories"):
            test_category = facets["categories"][0]["name"]
            print(f"\n\n5. Testing search by category '{test_category}':")
            category_results = await search_service.search(
                query="", category=test_category, page=1, per_page=5
            )

            print(
                f"Documents in '{test_category}' category: {category_results['total_count']}"
            )

            for i, doc in enumerate(category_results["documents"], 1):
                print(f"\n  Category Document {i}:")
                print(f"    ID: {doc['id']}")
                print(f"    Filename: {doc['filename']}")
                print(f"    Keywords: {doc['keywords']}")
                print(f"    Categories: {doc['categories']}")

        print("\n" + "=" * 60)
        print("Search API test completed successfully!")

    except Exception as e:
        print(f"Error during search API test: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(test_search_api())
