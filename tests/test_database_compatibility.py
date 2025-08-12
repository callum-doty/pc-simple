#!/usr/bin/env python3
"""
Test script to verify database compatibility with enhanced PromptManager analysis data
"""

import asyncio
import logging
import sys
import json
from pathlib import Path

# Add the simplified_app directory to the Python path
sys.path.insert(0, str(Path(__file__).parent))

from services.document_service import DocumentService
from models.document import Document, DocumentStatus
from database import init_db

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def test_database_schema():
    """Test that the database schema supports enhanced analysis data"""
    print("=" * 60)
    print("Testing Database Schema Compatibility")
    print("=" * 60)

    try:
        # Initialize database
        await init_db()
        print("✓ Database initialized successfully")

        # Initialize document service
        doc_service = DocumentService()
        print("✓ DocumentService initialized")

        return True

    except Exception as e:
        print(f"✗ Database schema test failed: {str(e)}")
        return False


async def test_enhanced_analysis_storage():
    """Test storing enhanced analysis data from PromptManager"""
    print("\n" + "=" * 60)
    print("Testing Enhanced Analysis Data Storage")
    print("=" * 60)

    try:
        doc_service = DocumentService()

        # Create a test document
        document = await doc_service.create_document(
            filename="test_enhanced_analysis.pdf",
            file_path="/test/path/test_enhanced_analysis.pdf",
            file_size=1024000,
        )
        print(f"✓ Created test document (ID: {document.id})")

        # Simulate unified analysis result from PromptManager
        unified_analysis = {
            "document_analysis": {
                "summary": "This is a comprehensive business report analyzing quarterly performance metrics.",
                "document_type": "report",
                "content_category": "business",
                "creation_year": 2024,
                "document_tone": "formal",
            },
            "classification": {
                "primary_purpose": "analytical",
                "subcategory": "Financial Report",
                "rationale": "Document contains financial data and performance analysis based on charts and numerical data.",
            },
            "entities": {
                "organization_name": "Acme Corporation",
                "author_name": "John Smith",
                "creation_date": "2024-03-15",
            },
            "analysis_provider": "anthropic",
            "analysis_type": "unified",
        }

        # Simulate modular analysis result
        modular_analysis = {
            "metadata": {
                "document_analysis": {
                    "summary": "Business report with financial data",
                    "document_type": "report",
                    "content_category": "business",
                    "creation_year": 2024,
                    "document_tone": "formal",
                }
            },
            "classification": {
                "classification": {
                    "primary_purpose": "analytical",
                    "subcategory": "Financial Report",
                    "rationale": "Contains financial analysis and metrics",
                }
            },
            "entities": {
                "entities": {
                    "organization_name": "Acme Corporation",
                    "author_name": "John Smith",
                    "creation_date": "2024-03-15",
                    "contact_info": "contact@acme.com",
                    "reference_number": "RPT-2024-Q1",
                }
            },
            "text_extraction": {
                "extracted_text": {
                    "title": "Q1 2024 Financial Report",
                    "main_content": "Revenue increased by 15% compared to previous quarter...",
                    "action_items": "Review budget allocation for Q2",
                }
            },
            "design_elements": {
                "design_elements": {
                    "color_scheme": ["#003366", "#FFFFFF", "#FF6600"],
                    "layout_style": "professional",
                    "document_format": "multi_page",
                    "branding_elements": "Acme Corporation logo",
                    "visual_elements": ["charts", "tables", "graphs"],
                }
            },
            "taxonomy_keywords": {
                "keyword_mappings": [
                    {
                        "verbatim_term": "quarterly revenue",
                        "mapped_primary_category": "Business & Finance",
                        "mapped_subcategory": "Financial Reporting",
                        "mapped_canonical_term": "Revenue",
                    },
                    {
                        "verbatim_term": "budget allocation",
                        "mapped_primary_category": "Business & Finance",
                        "mapped_subcategory": "Financial Planning",
                        "mapped_canonical_term": "Budget",
                    },
                ]
            },
            "communication_focus": {
                "communication_focus": {
                    "primary_topic": "Financial Performance",
                    "secondary_topics": ["Budget Planning", "Revenue Analysis"],
                    "communication_strategy": "analytical",
                    "target_audience": "Executive team and stakeholders",
                }
            },
            "analysis_provider": "anthropic",
            "analysis_type": "modular",
        }

        # Test storing unified analysis
        success = await doc_service.update_document_content(
            document.id,
            extracted_text="This is the extracted text from the document...",
            ai_analysis=unified_analysis,
            keywords=["revenue", "financial", "quarterly", "business"],
            categories=["business", "report", "analytical"],
            file_type="pdf",
            page_count=25,
        )

        if success:
            print("✓ Unified analysis data stored successfully")
        else:
            print("✗ Failed to store unified analysis data")
            return False

        # Retrieve and verify the stored data
        retrieved_doc = await doc_service.get_document(document.id)
        if retrieved_doc:
            print("✓ Document retrieved successfully")

            # Check AI analysis
            if retrieved_doc.ai_analysis:
                print(f"  - AI Analysis keys: {list(retrieved_doc.ai_analysis.keys())}")
                print(
                    f"  - Document type: {retrieved_doc.ai_analysis.get('document_analysis', {}).get('document_type')}"
                )
                print(
                    f"  - Summary: {retrieved_doc.ai_analysis.get('document_analysis', {}).get('summary', '')[:50]}..."
                )

            # Check keywords
            if retrieved_doc.keywords:
                print(f"  - Keywords: {retrieved_doc.get_keyword_list()}")
                print(f"  - Categories: {retrieved_doc.get_categories()}")

            # Check metadata
            if retrieved_doc.file_metadata:
                print(f"  - File type: {retrieved_doc.get_metadata('file_type')}")
                print(f"  - Page count: {retrieved_doc.get_metadata('page_count')}")

        # Test storing modular analysis (more complex structure)
        document2 = await doc_service.create_document(
            filename="test_modular_analysis.pdf",
            file_path="/test/path/test_modular_analysis.pdf",
            file_size=2048000,
        )

        success2 = await doc_service.update_document_content(
            document2.id,
            extracted_text="Extracted text from modular analysis document...",
            ai_analysis=modular_analysis,
            keywords=["revenue", "budget", "financial planning", "Q1 2024"],
            categories=["business", "financial", "analytical", "report"],
            file_type="pdf",
            page_count=35,
            analysis_type="modular",
        )

        if success2:
            print("✓ Modular analysis data stored successfully")

            # Retrieve and verify modular analysis
            retrieved_doc2 = await doc_service.get_document(document2.id)
            if retrieved_doc2 and retrieved_doc2.ai_analysis:
                print(
                    f"  - Modular analysis sections: {list(retrieved_doc2.ai_analysis.keys())}"
                )
                print(
                    f"  - Analysis type: {retrieved_doc2.ai_analysis.get('analysis_type')}"
                )

                # Check taxonomy keywords
                taxonomy_data = retrieved_doc2.ai_analysis.get("taxonomy_keywords", {})
                if taxonomy_data and "keyword_mappings" in taxonomy_data:
                    mappings = taxonomy_data["keyword_mappings"]
                    print(f"  - Taxonomy mappings: {len(mappings)} keywords mapped")
        else:
            print("✗ Failed to store modular analysis data")
            return False

        print("\n✓ All enhanced analysis data stored and retrieved successfully")
        return True

    except Exception as e:
        print(f"✗ Enhanced analysis storage test failed: {str(e)}")
        return False


async def test_json_field_flexibility():
    """Test JSON field flexibility for different analysis structures"""
    print("\n" + "=" * 60)
    print("Testing JSON Field Flexibility")
    print("=" * 60)

    try:
        doc_service = DocumentService()

        # Test with different analysis structures
        test_cases = [
            {
                "name": "Minimal Analysis",
                "data": {"summary": "Simple document", "document_type": "other"},
            },
            {
                "name": "Complex Nested Analysis",
                "data": {
                    "document_analysis": {
                        "summary": "Complex document with nested data",
                        "metadata": {
                            "confidence": 0.95,
                            "processing_time": 2.3,
                            "model_version": "claude-3-sonnet-20240229",
                        },
                    },
                    "classification": {
                        "categories": ["primary", "secondary"],
                        "confidence_scores": [0.9, 0.7],
                    },
                    "custom_fields": {
                        "experimental_feature": True,
                        "custom_tags": ["tag1", "tag2", "tag3"],
                    },
                },
            },
            {
                "name": "Array-Heavy Analysis",
                "data": {
                    "keywords": ["keyword1", "keyword2", "keyword3"],
                    "entities": [
                        {"name": "Entity1", "type": "PERSON", "confidence": 0.9},
                        {"name": "Entity2", "type": "ORG", "confidence": 0.8},
                    ],
                    "topics": [
                        {"topic": "Finance", "weight": 0.7},
                        {"topic": "Technology", "weight": 0.3},
                    ],
                },
            },
        ]

        for i, test_case in enumerate(test_cases):
            document = await doc_service.create_document(
                filename=f"test_json_flexibility_{i}.pdf",
                file_path=f"/test/path/test_json_flexibility_{i}.pdf",
                file_size=1024 * (i + 1),
            )

            success = await doc_service.update_document_content(
                document.id, ai_analysis=test_case["data"]
            )

            if success:
                # Retrieve and verify
                retrieved = await doc_service.get_document(document.id)
                if retrieved and retrieved.ai_analysis:
                    print(f"✓ {test_case['name']}: Stored and retrieved successfully")
                    print(
                        f"  - Data size: {len(json.dumps(retrieved.ai_analysis))} characters"
                    )
                else:
                    print(f"✗ {test_case['name']}: Failed to retrieve")
                    return False
            else:
                print(f"✗ {test_case['name']}: Failed to store")
                return False

        print("\n✓ JSON field flexibility test passed")
        return True

    except Exception as e:
        print(f"✗ JSON flexibility test failed: {str(e)}")
        return False


async def main():
    """Run all database compatibility tests"""
    print("Starting Database Compatibility Tests for PromptManager Integration")
    print("=" * 80)

    # Run tests
    tests = [
        ("Database Schema", test_database_schema),
        ("Enhanced Analysis Storage", test_enhanced_analysis_storage),
        ("JSON Field Flexibility", test_json_field_flexibility),
    ]

    results = []
    for test_name, test_func in tests:
        try:
            result = await test_func()
            results.append((test_name, result))
        except Exception as e:
            print(f"✗ {test_name} test crashed: {str(e)}")
            results.append((test_name, False))

    # Summary
    print("\n" + "=" * 80)
    print("DATABASE COMPATIBILITY TEST SUMMARY")
    print("=" * 80)

    passed = 0
    for test_name, result in results:
        status = "PASS" if result else "FAIL"
        print(f"{test_name}: {status}")
        if result:
            passed += 1

    print(f"\nPassed: {passed}/{len(results)} tests")

    if passed == len(results):
        print(
            "\n🎉 Database is fully compatible with PromptManager enhanced analysis data!"
        )
        print("\nKey Database Features Supporting Enhanced Analysis:")
        print("- ✅ JSON fields for flexible analysis data storage")
        print("- ✅ Separate fields for extracted text, keywords, and metadata")
        print("- ✅ Support for both unified and modular analysis structures")
        print("- ✅ Flexible schema accommodating future analysis enhancements")
        return 0
    else:
        print(
            f"\n❌ {len(results) - passed} test(s) failed. Database may need schema updates."
        )
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
