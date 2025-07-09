#!/usr/bin/env python3
"""
Test script to diagnose AI client initialization issues
"""

import os
import sys
import traceback
from pathlib import Path

# Add the simplified_app directory to Python path
sys.path.insert(0, str(Path(__file__).parent))

from config import get_settings


def test_anthropic_client():
    """Test Anthropic client initialization"""
    print("=== Testing Anthropic Client ===")

    try:
        import anthropic

        print(
            f"✓ Anthropic library imported successfully (version: {anthropic.__version__})"
        )

        settings = get_settings()
        print(f"✓ Settings loaded, API key present: {bool(settings.anthropic_api_key)}")

        if settings.anthropic_api_key:
            print(f"✓ API key starts with: {settings.anthropic_api_key[:10]}...")

            # Test different initialization methods
            try:
                client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
                print(
                    "✓ Anthropic client initialized successfully with anthropic.Anthropic()"
                )
                return client
            except Exception as e:
                print(f"✗ Failed with anthropic.Anthropic(): {e}")
                print(f"Error type: {type(e).__name__}")
                traceback.print_exc()

                # Try alternative initialization
                try:
                    client = anthropic.Client(api_key=settings.anthropic_api_key)
                    print(
                        "✓ Anthropic client initialized successfully with anthropic.Client()"
                    )
                    return client
                except Exception as e2:
                    print(f"✗ Failed with anthropic.Client(): {e2}")
                    print(f"Error type: {type(e2).__name__}")
                    traceback.print_exc()
        else:
            print("✗ No Anthropic API key found")

    except ImportError as e:
        print(f"✗ Failed to import anthropic: {e}")
    except Exception as e:
        print(f"✗ Unexpected error: {e}")
        traceback.print_exc()

    return None


def test_openai_client():
    """Test OpenAI client initialization"""
    print("\n=== Testing OpenAI Client ===")

    try:
        import openai

        print(f"✓ OpenAI library imported successfully (version: {openai.__version__})")

        settings = get_settings()
        print(f"✓ Settings loaded, API key present: {bool(settings.openai_api_key)}")

        if settings.openai_api_key:
            print(f"✓ API key starts with: {settings.openai_api_key[:10]}...")

            # Test different initialization methods
            try:
                client = openai.OpenAI(api_key=settings.openai_api_key)
                print("✓ OpenAI client initialized successfully with openai.OpenAI()")
                return client
            except Exception as e:
                print(f"✗ Failed with openai.OpenAI(): {e}")
                print(f"Error type: {type(e).__name__}")
                traceback.print_exc()

                # Try alternative initialization
                try:
                    client = openai.Client(api_key=settings.openai_api_key)
                    print(
                        "✓ OpenAI client initialized successfully with openai.Client()"
                    )
                    return client
                except Exception as e2:
                    print(f"✗ Failed with openai.Client(): {e2}")
                    print(f"Error type: {type(e2).__name__}")
                    traceback.print_exc()
        else:
            print("✗ No OpenAI API key found")

    except ImportError as e:
        print(f"✗ Failed to import openai: {e}")
    except Exception as e:
        print(f"✗ Unexpected error: {e}")
        traceback.print_exc()

    return None


def test_ai_service():
    """Test the AIService class initialization"""
    print("\n=== Testing AIService Class ===")

    try:
        from services.ai_service import AIService

        print("✓ AIService imported successfully")

        ai_service = AIService()
        print("✓ AIService initialized successfully")

        info = ai_service.get_ai_info()
        print(f"✓ AI Info: {info}")

        return ai_service

    except Exception as e:
        print(f"✗ Failed to initialize AIService: {e}")
        print(f"Error type: {type(e).__name__}")
        traceback.print_exc()

    return None


def test_simple_api_call(client, provider):
    """Test a simple API call"""
    print(f"\n=== Testing {provider} API Call ===")

    try:
        if provider == "anthropic":
            response = client.messages.create(
                model="claude-3-haiku-20240307",
                max_tokens=50,
                messages=[{"role": "user", "content": "Say hello"}],
            )
            print(f"✓ Anthropic API call successful: {response.content[0].text}")

        elif provider == "openai":
            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                max_tokens=50,
                messages=[{"role": "user", "content": "Say hello"}],
            )
            print(
                f"✓ OpenAI API call successful: {response.choices[0].message.content}"
            )

    except Exception as e:
        print(f"✗ API call failed: {e}")
        print(f"Error type: {type(e).__name__}")
        traceback.print_exc()


def main():
    """Main test function"""
    print("AI Client Diagnostic Test")
    print("=" * 50)

    # Test environment
    print(f"Python version: {sys.version}")
    print(f"Working directory: {os.getcwd()}")
    print(f"Environment: {os.getenv('ENVIRONMENT', 'not set')}")

    # Test Anthropic
    anthropic_client = test_anthropic_client()
    if anthropic_client:
        test_simple_api_call(anthropic_client, "anthropic")

    # Test OpenAI
    openai_client = test_openai_client()
    if openai_client:
        test_simple_api_call(openai_client, "openai")

    # Test AIService
    ai_service = test_ai_service()

    print("\n" + "=" * 50)
    print("Test completed!")


if __name__ == "__main__":
    main()
