# Document Reprocessing Feature

## Overview

Added the ability to reprocess incomplete documents directly from the admin dashboard. This feature allows administrators to reset documents with missing data (due to AI processing failures like API quota exceeded) back to QUEUED status for full reprocessing.

## Implementation Date

December 2, 2025

## Problem Addressed

When documents fail during AI processing (e.g., API 429 errors), they are marked as COMPLETED but have missing critical data such as:

- Summary
- Extracted text
- Keywords
- Embeddings

These documents need a way to be reprocessed without manual intervention or database manipulation.

## Solution Components

### 1. Backend Service Method (`services/document_service.py`)

Added `reset_document_for_reprocessing(document_id: int)` method that:

- Loads the document by ID
- Clears all AI-generated data fields:
  - `extracted_text` → NULL
  - `ai_analysis` → NULL
  - `keywords` → NULL
  - `search_vector` → NULL
- Clears taxonomy term associations
- Resets document status to `QUEUED`
- Clears `processing_error` field
- Resets `progress` to 0
- Clears `processed_at` timestamp
- Updates `updated_at` timestamp
- Returns success/failure boolean

### 2. API Endpoint (`api/documents.py`)

New endpoint: `POST /api/documents/{document_id}/reprocess`

**Request:**

- Method: POST
- Path: `/api/documents/{document_id}/reprocess`
- Parameters: `document_id` (path parameter)

**Response (Success):**

```json
{
  "success": true,
  "message": "Document {document_id} has been reset and queued for reprocessing",
  "document_id": 123
}
```

**Response (Error):**

- 404: Document not found or could not be reset
- 500: Internal server error

### 3. Dashboard UI (`templates/admin/dashboard.html`)

#### UI Components

**Reprocess Button:**

- Added to each document card in the incomplete documents panel
- Styled as a green button with hover effects
- Positioned next to the status badge
- Shows "Reprocessing..." when active
- Disabled during API call to prevent duplicate requests

**Visual Design:**

- Green background color (#28a745) for positive action
- Hover state with darker green (#218838)
- Disabled state with gray background (#6c757d)
- Smooth fade-out animation on success

#### JavaScript Functionality

**`reprocessDocument(documentId)` function:**

1. Disables the button and shows loading text
2. Calls the reprocess API endpoint
3. On success:
   - Fades out the document card (0.5s transition)
   - Removes the card from DOM
   - Reloads incomplete documents data to update counts
4. On error:
   - Shows inline error message
   - Re-enables the button
   - Allows retry

### 4. Processing Flow

1. User clicks "Reprocess" button on incomplete document
2. Frontend calls `POST /api/documents/{id}/reprocess`
3. Backend service clears all AI data and resets status to QUEUED
4. Document disappears from incomplete documents list
5. Existing scheduler picks up QUEUED document automatically
6. Celery worker processes document with full AI analysis
7. Once complete, document has all required data

## Features

### Individual Document Control

- Each document has its own "Reprocess" button
- No batch operations (keeps it simple and safe)
- Immediate visual feedback

### Full Data Refresh

- All AI-generated data is cleared
- Document is completely reprocessed from scratch
- No partial data is retained

### Seamless Integration

- Uses existing processing infrastructure
- No new worker tasks needed
- Automatic pickup by scheduler
- Respects existing rate limiting and queue management

### User Experience

- No confirmation dialogs (direct action)
- Loading state during processing
- Success: Document fades out and disappears
- Error: Inline error message with retry option
- Counts automatically update after reprocessing

## API Response Examples

### Success Response

```json
{
  "success": true,
  "message": "Document 123 has been reset and queued for reprocessing",
  "document_id": 123
}
```

### Error Response (Not Found)

```json
{
  "detail": "Document not found or could not be reset"
}
```

## Usage Instructions

1. Navigate to `/admin/dashboard`
2. Scroll to "Incomplete Documents" panel
3. Click "View Details" on any metric card to see documents
4. Click "Reprocess" button on any document
5. Wait for document to fade out (indicates success)
6. Document will be automatically reprocessed by scheduler
7. Verify document has complete data after processing

## Technical Details

### Database Changes

When a document is reset for reprocessing:

```python
# Fields cleared
document.extracted_text = None
document.ai_analysis = None
document.keywords = None
document.search_vector = None

# Status reset
document.status = DocumentStatus.QUEUED
document.processing_error = None
document.progress = 0
document.processed_at = None
document.updated_at = datetime.utcnow()

# Associations cleared
document.taxonomy_terms.clear()
```

### Processing Timeline

1. **T+0s**: User clicks "Reprocess" button
2. **T+0.5s**: API call completes, document status = QUEUED
3. **T+0.5s**: Document fades out of UI
4. **T+1s**: Document removed from DOM
5. **T+2-120s**: Scheduler picks up document (runs every 2 minutes)
6. **T+varies**: Worker processes document (depends on queue)
7. **T+final**: Document marked COMPLETED with all data

## Security Considerations

- No password protection on reprocess endpoint
- Only accessible from authenticated admin dashboard
- Protected by existing authentication middleware
- Rate limiting applies to all API endpoints
- No batch operations to prevent system overload

## Performance Considerations

- Individual reprocessing only (no bulk operations)
- Uses existing Celery infrastructure
- No additional database load
- Minimal UI updates (single document removal)
- Redis cache cleared automatically after processing

## Testing

To test the feature:

1. Create a document with missing data (or manually clear data in database)
2. Verify document appears in incomplete documents panel
3. Click "Reprocess" button
4. Verify button shows "Reprocessing..."
5. Verify document fades out and disappears
6. Verify counts decrease by 1
7. Check database to confirm status = QUEUED
8. Wait for scheduler to pick up document
9. Verify document processes successfully
10. Verify document no longer appears in incomplete list

## Future Enhancements

Potential improvements:

1. Batch reprocessing with checkboxes
2. Reprocessing history/audit log
3. Estimated time until processing starts
4. Real-time status updates during processing
5. Automatic reprocessing when API quota restored
6. Email notifications on reprocessing completion
7. Selective reprocessing (only missing fields)
8. Reprocessing queue with priority

## Code Changes Summary

### Files Modified

1. `services/document_service.py`

   - Added `reset_document_for_reprocessing()` method

2. `api/documents.py`

   - Added `POST /api/documents/{document_id}/reprocess` endpoint

3. `templates/admin/dashboard.html`
   - Added CSS for `.reprocess-btn`
   - Updated `renderDocumentList()` to include reprocess button
   - Added `reprocessDocument()` JavaScript function

### Files Created

1. `docs/DOCUMENT_REPROCESSING_FEATURE.md` (this file)

## Rollback Instructions

If the feature needs to be removed:

1. Remove the "Reprocess" button from `renderDocumentList()` in dashboard.html
2. Remove the `reprocessDocument()` function from dashboard.html
3. Remove the `.reprocess-btn` CSS rules from dashboard.html
4. Remove the `/reprocess` endpoint from `api/documents.py`
5. Remove `reset_document_for_reprocessing()` method from `document_service.py`

The feature is completely non-destructive and can be safely removed without affecting existing data.

## Conclusion

The document reprocessing feature provides a simple, effective way to recover from AI processing failures. It integrates seamlessly with existing infrastructure and provides immediate visual feedback to administrators. The implementation is minimal, non-invasive, and follows existing patterns in the codebase.
