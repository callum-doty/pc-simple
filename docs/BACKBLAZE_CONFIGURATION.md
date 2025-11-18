# Backblaze B2 Configuration for Preview Images

This document provides detailed instructions for configuring Backblaze B2 to properly serve preview images and documents with presigned URLs.

## Issue Background

The application generates presigned URLs for Backblaze B2 storage, but browsers were receiving 401 Unauthorized errors when trying to access preview images. This occurred because:

1. Bucket CORS configuration was missing or incomplete
2. Presigned URL parameters didn't include proper content-type and disposition headers
3. No fallback mechanism existed if presigned URLs failed

## Solution Overview

The fix involves both **Backblaze B2 configuration** and **application code updates**:

### 1. Backblaze B2 Configuration (Required)

Configure your Backblaze B2 bucket with proper CORS rules to allow browser access to presigned URLs.

### 2. Application Code Updates (Completed)

- Enhanced presigned URL generation with proper headers
- Added failsafe streaming fallback
- Improved error handling and logging

---

## Backblaze B2 Configuration Steps

### Step 1: Access Bucket Settings

1. Log into your Backblaze B2 account
2. Navigate to **Buckets** in the left sidebar
3. Click on your bucket (e.g., `documents80047`)
4. Click on **Bucket Settings**

### Step 2: Configure Bucket Privacy

**Recommended Setting: Private**

- Keep the bucket as **Private** for security
- Files will be accessible only via presigned URLs with proper authentication
- This is more secure than making the bucket public

### Step 3: Configure CORS Rules

CORS (Cross-Origin Resource Sharing) rules are **required** for browsers to access files via presigned URLs.

1. In Bucket Settings, scroll to **CORS Rules**
2. Click **Add CORS Rule** or **Edit CORS Rules**
3. Add the following CORS configuration:

```json
[
  {
    "corsRuleName": "allowDirectAccess",
    "allowedOrigins": ["*"],
    "allowedOperations": ["s3_get"],
    "allowedHeaders": ["*"],
    "exposeHeaders": ["ETag", "x-amz-request-id"],
    "maxAgeSeconds": 3600
  }
]
```

#### CORS Configuration Explained

- **corsRuleName**: Friendly name for the rule
- **allowedOrigins**: `["*"]` allows all origins (you can restrict to your domain if needed: `["https://yourdomain.com"]`)
- **allowedOperations**: `["s3_get"]` allows GET requests (read-only)
- **allowedHeaders**: `["*"]` allows all request headers
- **exposeHeaders**: Headers the browser can access in the response
- **maxAgeSeconds**: How long browsers cache CORS preflight responses (1 hour)

#### More Restrictive CORS (Optional)

For production, you may want to restrict to your specific domain:

```json
[
  {
    "corsRuleName": "allowYourDomain",
    "allowedOrigins": ["https://your-app-domain.com"],
    "allowedOperations": ["s3_get"],
    "allowedHeaders": [
      "authorization",
      "content-type",
      "x-amz-content-sha256",
      "x-amz-date",
      "x-amz-security-token",
      "x-amz-user-agent"
    ],
    "exposeHeaders": ["ETag", "x-amz-request-id"],
    "maxAgeSeconds": 3600
  }
]
```

### Step 4: Verify Configuration

1. Save the CORS rules
2. Wait 5-10 minutes for the configuration to propagate
3. Test by accessing a preview URL in your application

---

## Application Code Changes

The following changes have been implemented in the application code:

### 1. Enhanced Presigned URL Generation

**File: `services/storage_service.py`**

Changes to `_get_s3_presigned_url()`:

- Added `ResponseContentType` parameter for explicit content-type
- Added `ResponseContentDisposition: inline` for preview images
- Added `ResponseCacheControl` for browser caching
- Enhanced error handling with detailed ClientError logging

**Key improvements:**

- Images (previews) now include `inline` disposition to view in browser
- Cache control headers enable browser caching for better performance
- Better error messages for debugging permission issues

### 2. Failsafe Streaming Fallback

**File: `main.py`**

Changes to `/previews/{filename}` endpoint:

- Attempts presigned URL redirect first (optimal performance)
- Falls back to direct streaming if presigned URL fails
- Added comprehensive error logging
- Returns placeholder image if file not found

**Behavior:**

1. For S3 with direct URLs enabled: Generate presigned URL → Redirect to Backblaze
2. If presigned URL generation fails: Stream file through application
3. If file not found: Serve placeholder image

### 3. Enhanced Error Handling

**Improvements:**

- Detailed logging of S3 ClientError exceptions
- Error codes and messages logged for debugging
- Graceful fallback prevents user-facing errors

---

## Testing the Configuration

### 1. Check Storage Health Endpoint

```bash
curl https://your-app-domain.com/health/storage
```

Expected response (when working):

```json
{
  "status": "optimized",
  "optimization_active": true,
  "storage_health": {
    "storage_type": "s3",
    "direct_urls_enabled": true,
    "preview_url_expires_hours": 24,
    "download_url_expires_hours": 1,
    "s3_presigned_url_generation": "working",
    "s3_bucket": "your-bucket-name",
    "s3_region": "us-east-005"
  }
}
```

### 2. Test Preview Access

1. Navigate to your application
2. Search for documents
3. Verify preview images load correctly
4. Check browser Developer Tools → Network tab for:
   - 302 redirect to Backblaze URL
   - 200 OK response from Backblaze (not 401)

### 3. Check Application Logs

Look for these log messages:

**Success:**

```
Generated presigned URL for previews/xxx_preview.png with content_type=image/png, expires in 86400s
Redirecting preview xxx_preview.png to direct URL
```

**Fallback (if presigned URL fails):**

```
Failed to generate presigned URL for xxx_preview.png, falling back to streaming
Streaming preview xxx_preview.png directly from storage
```

**Error (if file not found):**

```
Preview file not found in storage: previews/xxx_preview.png
Serving placeholder for missing preview: xxx_preview.png
```

---

## Troubleshooting

### Issue: Still getting 401 Unauthorized

**Causes:**

1. CORS rules not properly configured
2. CORS rules not yet propagated (wait 5-10 minutes)
3. S3 credentials incorrect or expired

**Solutions:**

1. Verify CORS rules in Backblaze dashboard
2. Check application logs for S3 errors
3. Verify environment variables: `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`, `S3_REGION`

### Issue: Images load slowly

**Causes:**

1. Presigned URL generation is failing, falling back to streaming
2. Network latency to Backblaze

**Solutions:**

1. Check `/health/storage` endpoint - should show "optimized" status
2. Verify CORS configuration is correct
3. Consider increasing `PREVIEW_URL_EXPIRES_HOURS` for longer cache times

### Issue: Some previews work, others don't

**Causes:**

1. Previews created before CORS configuration
2. Inconsistent file permissions

**Solutions:**

1. Wait for CORS to propagate (5-10 minutes)
2. Regenerate previews by reprocessing documents
3. Check logs for specific files failing

---

## Environment Variables

Ensure these environment variables are properly set:

```bash
# Storage configuration
STORAGE_TYPE=s3
S3_BUCKET=your-bucket-name
S3_ACCESS_KEY=your-access-key-id
S3_SECRET_KEY=your-secret-access-key
S3_REGION=us-east-005  # Or your region

# Direct URL optimization
USE_DIRECT_URLS=true
PREVIEW_URL_EXPIRES_HOURS=24
DOWNLOAD_URL_EXPIRES_HOURS=1
```

---

## Security Considerations

### Private Bucket with Presigned URLs

**Recommended Approach:**

- Keep bucket **Private**
- Use presigned URLs with expiration
- Application authenticates users, then generates time-limited URLs

**Benefits:**

- Files not publicly accessible without valid presigned URL
- URLs expire automatically (24 hours for previews, 1 hour for downloads)
- Better control over access

### Public Bucket (Not Recommended)

If you need fully public access:

1. Set bucket to **Public**
2. Disable presigned URLs: `USE_DIRECT_URLS=false`
3. Application will stream all files

**Drawbacks:**

- Anyone with URL can access files
- No expiration or access control
- Less secure

---

## Performance Optimization

### Current Configuration

- **Previews**: 24-hour presigned URLs with browser caching
- **Downloads**: 1-hour presigned URLs
- **Fallback**: Direct streaming if presigned URLs fail

### Benefits

1. **Direct URLs**: Browser loads from Backblaze directly, not through app
2. **Browser Caching**: Previews cached for 24 hours
3. **Failsafe**: Streaming fallback ensures reliability

### Tuning

Adjust expiration times in `.env`:

```bash
# Longer expiration = fewer URL regenerations, more caching
PREVIEW_URL_EXPIRES_HOURS=72  # 3 days

# Shorter expiration = better security, more frequent regeneration
DOWNLOAD_URL_EXPIRES_HOURS=0.5  # 30 minutes
```

---

## Summary

1. ✅ Configure Backblaze B2 CORS rules (required)
2. ✅ Application code updated with enhanced presigned URLs
3. ✅ Failsafe streaming fallback implemented
4. ✅ Comprehensive error handling added
5. ✅ Test using `/health/storage` endpoint

The combination of proper CORS configuration and enhanced application code ensures reliable preview loading with optimal performance.
