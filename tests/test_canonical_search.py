#!/usr/bin/env python3
"""
Simple test to verify canonical term search functionality
"""

import asyncio
import json
import re
from database import SessionLocal, init_db
from models.document import Document
from sqlalchemy import text, func
from sqlalchemy.dialects.postgresql import JSONB


async def test_canonical_search():
    """Test canonical term search with different field name variations"""

    await init_db()
    db = SessionLocal()

    try:
        print("=== TESTING CANONICAL TERM SEARCH ===\n")

        # Test different field name variations for "Taxes"
        search_term = "Taxes"
        pattern = f"^{re.escape(search_term)}$"

        field_variations = [
            "mapped_canonical_term",
            "canonical_term",
            "term",
            "canonical",
        ]

        for field_name in field_variations:
            print(f"Testing field: {field_name}")

            # Test with case-insensitive regex
            path_expr = (
                f'$.keyword_mappings[*] ? (@.{field_name} like_regex $term flag "i")'
            )

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

                print(f"  Results with regex: {len(results)} documents")

                # Also test with exact match
                path_expr_exact = f"$.keyword_mappings[*] ? (@.{field_name} == $term)"
                results_exact = (
                    db.query(Document)
                    .filter(
                        text(
                            "jsonb_path_exists(documents.keywords, :path::jsonpath, :vars::jsonb)"
                        )
                    )
                    .params(
                        path=path_expr_exact, vars=json.dumps({"term": search_term})
                    )
                    .all()
                )

                print(f"  Results with exact: {len(results_exact)} documents")

                if results or results_exact:
                    print(f"  *** FOUND MATCHES FOR FIELD '{field_name}' ***")

            except Exception as e:
                print(f"  Error: {str(e)}")

            print()

        # Also test what canonical terms actually exist
        print("=== CHECKING EXISTING CANONICAL TERMS ===")

        try:
            # Use jsonb_array_elements to find all canonical terms
            keyword_element = func.jsonb_array_elements(
                func.coalesce(
                    Document.keywords.op("#>")("{keyword_mappings}"),
                    func.cast("[]", JSONB),
                )
            ).alias("keyword_element")

            for field_name in field_variations:
                try:
                    canonical_terms = (
                        db.query(
                            keyword_element.c.value[field_name].astext,
                            func.count(),
                        )
                        .select_from(Document, keyword_element)
                        .filter(
                            keyword_element.c.value[field_name].isnot(None),
                        )
                        .group_by(keyword_element.c.value[field_name].astext)
                        .order_by(func.count().desc())
                        .limit(10)
                        .all()
                    )

                    if canonical_terms:
                        print(
                            f"\nField '{field_name}' contains {len(canonical_terms)} unique terms:"
                        )
                        for term, count in canonical_terms:
                            print(f"  - '{term}' ({count} docs)")
                            if term and "tax" in term.lower():
                                print(f"    *** TAX-RELATED TERM! ***")
                    else:
                        print(f"\nField '{field_name}': No terms found")

                except Exception as e:
                    print(f"\nField '{field_name}': Error - {str(e)}")

        except Exception as e:
            print(f"Error in canonical terms check: {str(e)}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()

    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(test_canonical_search())
