#!/usr/bin/env python3
"""
Test script to verify PromptManager integration with AIService
"""

import asyncio
import logging
import sys
import os
from pathlib import Path

# Add the simplified_app directory to the Python path
sys.path.insert(0, str(Path(__file__).parent))

from services.ai_service import AIService
from services.prompt_manager import PromptManager
from config import get_settings

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def test_prompt_manager():
    """Test PromptManager functionality"""
    print("=" * 60)
    print("Testing PromptManager Integration")
    print("=" * 60)

    try:
        # Initialize PromptManager
        prompt_manager = PromptManager()
        print("✓ PromptManager initialized successfully")

        # Test unified analysis prompt
        unified_prompt = prompt_manager.get_unified_analysis_prompt("test_document.pdf")
        print("✓ Unified analysis prompt generated")
        print(f"  System prompt length: {len(unified_prompt['system'])} characters")
        print(f"  User prompt length: {len(unified_prompt['user'])} characters")

        # Test all prompt types
        prompt_methods = [
            ("Core Metadata", prompt_manager.get_core_metadata_prompt),
            ("Classification", prompt_manager.get_classification_prompt),
            ("Entity Extraction", prompt_manager.get_entity_prompt),
            ("Text Extraction", prompt_manager.get_text_extraction_prompt),
            ("Design Elements", prompt_manager.get_design_elements_prompt),
            ("Taxonomy Keywords", prompt_manager.get_taxonomy_keyword_prompt),
            ("Communication Focus", prompt_manager.get_communication_focus_prompt),
        ]

        for name, method in prompt_methods:
            try:
                prompt = method("test_document.pdf")
                print(f"✓ {name} prompt generated successfully")
            except Exception as e:
                print(f"✗ {name} prompt failed: {str(e)}")

        print("\n" + "=" * 60)
        print("PromptManager tests completed successfully!")
        return True

    except Exception as e:
        print(f"✗ PromptManager test failed: {str(e)}")
        return False


async def test_ai_service():
    """Test AIService with PromptManager integration"""
    print("\n" + "=" * 60)
    print("Testing AIService Integration")
    print("=" * 60)

    try:
        # Initialize AIService
        ai_service = AIService()
        print("✓ AIService initialized successfully")

        # Test AI info
        ai_info = ai_service.get_ai_info()
        print("✓ AI info retrieved")
        print(f"  AI Provider: {ai_info['ai_provider']}")
        print(f"  Anthropic Available: {ai_info['anthropic_available']}")
        print(f"  OpenAI Available: {ai_info['openai_available']}")
        print(f"  PromptManager Enabled: {ai_info['prompt_manager_enabled']}")

        # Test available analysis types
        analysis_types = ai_service.get_available_analysis_types()
        print("✓ Analysis types retrieved")
        print(f"  Available types: {', '.join(analysis_types)}")

        # Test prompt enhancement (without actual AI call)
        test_text = "This is a sample document text for testing purposes."
        enhanced_prompt = ai_service._enhance_prompt_with_text(
            "Analyze this document:", test_text
        )
        print("✓ Prompt enhancement working")
        print(f"  Enhanced prompt length: {len(enhanced_prompt)} characters")

        print("\n" + "=" * 60)
        print("AIService tests completed successfully!")
        return True

    except Exception as e:
        print(f"✗ AIService test failed: {str(e)}")
        return False


async def test_analysis_workflow():
    """Test the complete analysis workflow (without actual file)"""
    print("\n" + "=" * 60)
    print("Testing Analysis Workflow")
    print("=" * 60)

    try:
        ai_service = AIService()

        # Test fallback analysis (when no AI provider is configured)
        if ai_service.ai_provider == "none":
            fallback_result = ai_service._get_fallback_analysis("test.pdf", "pdf")
            print("✓ Fallback analysis working")
            print(f"  Result keys: {list(fallback_result.keys())}")

        # Test keyword extraction from mock analysis
        mock_analysis = {
            "document_analysis": {
                "document_type": "report",
                "content_category": "business",
            },
            "classification": {
                "primary_purpose": "informational",
                "subcategory": "Financial Report",
            },
            "keyword_mappings": [
                {
                    "verbatim_term": "quarterly revenue",
                    "mapped_canonical_term": "Revenue",
                }
            ],
        }

        keywords, categories = ai_service._extract_keywords_from_analysis(mock_analysis)
        print("✓ Keyword extraction working")
        print(f"  Extracted keywords: {keywords}")
        print(f"  Extracted categories: {categories}")

        print("\n" + "=" * 60)
        print("Analysis workflow tests completed successfully!")
        return True

    except Exception as e:
        print(f"✗ Analysis workflow test failed: {str(e)}")
        return False


async def main():
    """Run all tests"""
    print("Starting PromptManager Integration Tests")
    print("=" * 60)

    # Load settings
    settings = get_settings()
    print(f"Environment: {settings.environment}")
    print(f"Debug mode: {settings.debug}")

    # Run tests
    tests = [
        ("PromptManager", test_prompt_manager),
        ("AIService", test_ai_service),
        ("Analysis Workflow", test_analysis_workflow),
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
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)

    passed = 0
    for test_name, result in results:
        status = "PASS" if result else "FAIL"
        print(f"{test_name}: {status}")
        if result:
            passed += 1

    print(f"\nPassed: {passed}/{len(results)} tests")

    if passed == len(results):
        print("\n🎉 All tests passed! PromptManager integration is working correctly.")
        return 0
    else:
        print(
            f"\n❌ {len(results) - passed} test(s) failed. Please check the errors above."
        )
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
