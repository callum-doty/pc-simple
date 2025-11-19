"""
Clear Redis cache to force fresh preview URL generation
"""

import redis
import os
from config import get_settings

settings = get_settings()


def clear_redis_cache():
    """Clear all search and facet caches from Redis"""
    try:
        if not settings.redis_url:
            print("❌ No Redis URL configured")
            return False

        redis_client = redis.from_url(settings.redis_url, decode_responses=True)
        redis_client.ping()
        print(f"✅ Connected to Redis at {settings.redis_url}")

        # Get all keys matching search patterns
        search_keys = redis_client.keys("search:*")
        facet_keys = redis_client.keys("facets:*")

        all_keys = search_keys + facet_keys

        if not all_keys:
            print("✅ No cached keys found - cache is already clear")
            return True

        print(f"📊 Found {len(all_keys)} cached keys to delete")
        print(f"   - {len(search_keys)} search cache keys")
        print(f"   - {len(facet_keys)} facet cache keys")

        # Delete all keys
        deleted = redis_client.delete(*all_keys)
        print(f"✅ Deleted {deleted} keys from Redis cache")

        return True

    except redis.exceptions.ConnectionError as e:
        print(f"❌ Could not connect to Redis: {e}")
        return False
    except Exception as e:
        print(f"❌ Error clearing cache: {e}")
        return False


def verify_config():
    """Verify configuration settings"""
    print("\n📋 Current Configuration:")
    print(f"   Storage Type: {settings.storage_type}")
    print(f"   S3 Bucket: {settings.s3_bucket}")
    print(f"   S3 Region: {settings.s3_region}")
    print(f"   USE_DIRECT_URLS: {settings.use_direct_urls}")
    print(f"   Preview URL Expires: {settings.preview_url_expires_hours} hours")
    print(f"   Environment: {settings.environment}")

    if settings.use_direct_urls:
        print("\n⚠️  WARNING: USE_DIRECT_URLS is still True!")
        print("   Presigned URLs will still be generated")
        print("   Set USE_DIRECT_URLS=false in Render environment variables")
    else:
        print("\n✅ USE_DIRECT_URLS is False - will use streaming")


if __name__ == "__main__":
    print("🔧 Redis Cache Clearing Script\n")

    verify_config()

    print("\n" + "=" * 60)
    response = input("\nClear Redis cache? (yes/no): ")

    if response.lower() in ["yes", "y"]:
        if clear_redis_cache():
            print("\n✅ Cache cleared successfully!")
            print("   New searches will generate fresh preview URLs")
        else:
            print("\n❌ Failed to clear cache")
    else:
        print("\n❌ Cache clearing cancelled")
