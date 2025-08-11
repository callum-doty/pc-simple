#!/usr/bin/env python3
"""
Debug script to examine keyword mappings structure and test search queries
"""

import asyncio
import json
from database import SessionLocal, init_db
from models.document import Document
from sqlalchemy import text, func
from sqlalchemy.dialects.postgresql import JSONB


async def debug_search_mappings():
    """Debug keyword mappings and search functionality"""

    # Initialize database
    await init_db()

    db = SessionLocal()

    try:
        print("=== DEBUGGING KEYWORD MAPPINGS AND SEARCH ===\n")

        # 1. Check total documents with keywords
        docs_with_keywords = (
            db.query(Document).filter(Document.keywords.isnot(None)).all()
        )

        print(f"Found {len(docs_with_keywords)} documents with keywords")
        print("=" * 80)

        # 2. Examine the actual structure of keyword mappings
        for i, doc in enumerate(docs_with_keywords[:3]):  # Look at first 3 documents
            print(f"\nDocument {i+1}: {doc.filename}")
            print(f"Document ID: {doc.id}")

            if doc.keywords:
                print("Full keywords JSON structure:")
                print(json.dumps(doc.keywords, indent=2))

                # Check if keyword_mappings exists
                if "keyword_mappings" in doc.keywords:
                    mappings = doc.keywords["keyword_mappings"]
                    print(f"\nFound {len(mappings)} keyword mappings:")

                    for j, mapping in enumerate(mappings[:3]):  # Show first 3 mappings
                        print(f"  Mapping {j+1}: {json.dumps(mapping, indent=4)}")

                        # Check field names
                        print(f"    Available fields: {list(mapping.keys())}")

                        # Look for canonical term variations
                        canonical_variations = [
                            "canonical_term",
                            "mapped_canonical_term",
                            "canonical",
                            "term",
                            "mapped_term",
                        ]
                        for var in canonical_variations:
                            if var in mapping:
                                print(
                                    f"    Found canonical term field '{var}': {mapping[var]}"
                                )
                else:
                    print("No 'keyword_mappings' found in keywords JSON")
            else:
                print("No keywords found")

            print("-" * 60)

        # 3. Test specific search for "Taxes"
        print(f"\n=== TESTING SEARCH FOR 'Taxes' ===")

        # Test different JSONPath variations
        test_patterns = [
            '$.keyword_mappings[*] ? (@.mapped_canonical_term like_regex $term flag "i")',
            '$.keyword_mappings[*] ? (@.canonical_term like_regex $term flag "i")',
            '$.keyword_mappings[*] ? (@.term like_regex $term flag "i")',
            "$.keyword_mappings[*] ? (@.mapped_canonical_term == $term)",
            "$.keyword_mappings[*] ? (@.canonical_term == $term)",
        ]

        search_term = "Taxes"
        pattern = f"^{search_term}$"

        for i, path_expr in enumerate(test_patterns):
            print(f"\nTest {i+1}: {path_expr}")
            try:
                results = (
                    db.query(Document)
                    .filter(
                        text(
                            "jsonb_path_exists(documents.keywords, :path::jsonpath, :vars::jsonb)"
                        )
                    )
                    .params(path=path_expr, vars=json.dumps({"term": pattern}))
                    .all()
                )

                print(f"  Results: {len(results)} documents found")
                if results:
                    for doc in results[:2]:  # Show first 2 results
                        print(f"    - {doc.filename} (ID: {doc.id})")
            except Exception as e:
                print(f"  Error: {str(e)}")

        # 4. Check what canonical terms actually exist in the database
        print(f"\n=== CANONICAL TERMS IN DATABASE ===")

        # Use jsonb_array_elements to unnest and find all canonical terms
        try:
            keyword_element = func.jsonb_array_elements(
                func.coalesce(
                    Document.keywords.op("#>")("{keyword_mappings}"),
                    func.cast("[]", JSONB),
                )
            ).alias("keyword_element")

            # Try different field name variations
            field_variations = [
                "mapped_canonical_term",
                "canonical_term",
                "term",
                "canonical",
            ]

            for field_name in field_variations:
                print(f"\nChecking field: {field_name}")
                try:
                    canonical_terms = (
                        db.query(
                            keyword_element.c.value[field_name].astext,
                            func.count(),
                        )
                        .select_from(Document, keyword_element)
                        .filter(
                            Document.status == "COMPLETED",
                            keyword_element.c.value[field_name].isnot(None),
                        )
                        .group_by(keyword_element.c.value[field_name].astext)
                        .order_by(func.count().desc())
                        .limit(10)
                        .all()
                    )

                    if canonical_terms:
                        print(f"  Found {len(canonical_terms)} unique terms:")
                        for term, count in canonical_terms:
                            print(f"    - '{term}' ({count} documents)")
                            if term and "tax" in term.lower():
                                print(f"      *** TAX-RELATED TERM FOUND! ***")
                    else:
                        print(f"  No terms found for field '{field_name}'")

                except Exception as e:
                    print(f"  Error querying field '{field_name}': {str(e)}")

        except Exception as e:
            print(f"Error in canonical terms analysis: {str(e)}")

        # 5. Raw SQL query to see the actual JSON structure
        print(f"\n=== RAW JSON STRUCTURE SAMPLE ===")
        try:
            raw_results = db.execute(
                text(
                    """
                SELECT filename, keywords->'keyword_mappings' as mappings 
                FROM documents 
                WHERE keywords->'keyword_mappings' IS NOT NULL 
                LIMIT 3
                """
                )
            ).fetchall()

            for filename, mappings in raw_results:
                print(f"\nDocument: {filename}")
                print(f"Raw mappings JSON: {mappings}")

        except Exception as e:
            print(f"Error in raw SQL query: {str(e)}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()

    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(debug_search_mappings())
