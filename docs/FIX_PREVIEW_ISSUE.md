# Fixing Preview Image Loading Issue

## Problem Summary

Preview images for new documents were failing to load with 401 Unauthorized errors. The issue had multiple contributing factors:

### Root Causes

1. **Presigned URL Expiration**: Backblaze B2's S3-compatible API generates presigned URLs with strict expiration times. These URLs were being stored in the database and search cache, but would expire while still being served.

2. **Batch Upload Processing Delays**: When uploading large batches (60+ documents), the 2-minute stagger between each document meant the last document wouldn't start processing for 2 hours. This long delay increased the likelihood of URL expiration issues.

3. **Cache Compounding**: Redis cached search results containing preview URLs for 1 hour, so even after fixing configuration, old expired URLs remained in cache.

4. **Database Storage of Expiring URLs**: The worker was storing presigned URLs directly in the database's `preview_url` field, which would then expire, causing 401 errors.

## Solution

### Step 1: Ensure USE_DIRECT_URLS is Set to False

In your Render dashboard:

1. Go to your service → Environment
2. Add or update: `USE_DIRECT_URLS=false`
3. This forces all previews to stream through your application instead of using presigned URLs

### Step 2: Clear Redis Cache

Once deployed, clear the Redis cache to remove expired presigned URLs:

**Using curl:**

```bash
curl -X POST https://document-catalog-app.onrender.com/api/admin/clear-cache \
  -F "password=upload123"
```

Replace `upload123` with your actual `UPLOAD_PASSWORD` value.

**Expected Response:**

```json
{
  "success": true,
  "message": "Successfully cleared 50 cache entries",
  "deleted_count": 50,
  "search_keys": 45,
  "facet_keys": 5,
  "use_direct_urls": false,
  "storage_type": "s3"
}
```

### Step 3: Verify Configuration

Check your deployment configuration at:

```
https://document-catalog-app.onrender.com/health/storage
```

Should show:

```json
{
  "status": "basic",
  "optimization_active": false,
  "storage_health": {
    "storage_type": "s3",
    "direct_urls_enabled": false,
    ...
  }
}
```

## How It Works After the Fix

### Before (Broken):

1. User searches for documents
2. Results include presigned URLs like: `https://s3.us-east-005.backblazeb2.com/...?X-Amz-Signature=...`
3. Browser tries to load preview from presigned URL
4. URL is expired → 401 Unauthorized
5. Preview fails to load

### After (Fixed):

1. User searches for documents
2. Results include app URLs like: `https://document-catalog-app.onrender.com/previews/xxx.png`
3. Browser requests preview from your app
4. Your app fetches file from Backblaze using boto3
5. Your app streams file to browser
6. ✅ Preview loads successfully

## Performance Considerations

**Streaming (USE_DIRECT_URLS=false):**

- ✅ Reliable - always works
- ✅ Secure - authentication handled by app
- ⚠️ Slower - files stream through your server
- ⚠️ Uses more bandwidth on your server

**Presigned URLs (USE_DIRECT_URLS=true):**

- ✅ Fast - browser loads directly from Backblaze
- ✅ Efficient - saves server bandwidth
- ❌ Currently broken with Backblaze B2 S3 API
- ❌ Requires proper CORS and authentication setup

## Why Presigned URLs Fail

Backblaze B2's S3-compatible API has strict requirements for presigned URLs:

1. Exact signature calculation including all headers
2. Proper CORS configuration for S3 API (not just B2 Native API)
3. Correct bucket privacy settings
4. Valid application key with proper permissions

The streaming fallback works reliably and is recommended until the presigned URL issues are fully resolved.

## Troubleshooting

### Cache not clearing?

- Verify your upload password is correct
- Check Redis is running: https://document-catalog-app.onrender.com/health/session
- Look at Render logs for errors

### Previews still not loading?

- Clear your browser cache (Cmd+Shift+R on Mac, Ctrl+Shift+R on Windows)
- Check USE_DIRECT_URLS is actually false in environment
- Verify Storage Health endpoint shows `direct_urls_enabled: false`
- Check Render logs for streaming messages: "Streaming preview xxx directly from storage"

### Old previews work but new ones don't?

- This was the original issue - old previews used app streaming
- New previews tried to use presigned URLs
- After clearing cache and setting USE_DIRECT_URLS=false, all should use streaming

## Files Changed

- `main.py`: Added `/api/admin/clear-cache` endpoint
- `clear_cache.py`: Local script for cache management (development only)
- `docs/FIX_PREVIEW_ISSUE.md`: This documentation

## Deployment Steps

1. Commit changes to git
2. Push to GitHub
3. Render auto-deploys
4. Set `USE_DIRECT_URLS=false` in Render environment
5. Call `/api/admin/clear-cache` endpoint
6. Test preview loading
