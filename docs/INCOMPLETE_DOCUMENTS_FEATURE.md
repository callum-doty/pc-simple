# Incomplete Documents Dashboard Feature

## Overview

Added a comprehensive dashboard section to identify and track documents with missing critical data due to AI processing failures (e.g., API quota exceeded, 429 errors).

## Implementation Date

December 2, 2025

## Problem Addressed

When the OpenAI API quota is exceeded (HTTP 429 error), documents get uploaded but fail during AI processing, leaving them without:

1. Summary
2. Extracted text
3. Keywords
4. Embeddings

This makes these documents unsearchable and incomplete in the system.

## Solution Components

### 1. Backend Service (`services/dashboard_service.py`)

Added `get_incomplete_documents()` method that:

- Queries the database for documents missing each type of data
- Returns counts and detailed lists (up to 100 documents per category)
- Calculates total unique incomplete documents
- Includes document metadata (ID, filename, status, timestamps, errors)

### 2. API Endpoint (`api/dashboard.py`)

New endpoint: `GET /api/incomplete-documents`

- Returns structured data about incomplete documents
- Organized by missing data type
- Includes document details for troubleshooting

### 3. Dashboard UI (`templates/admin/dashboard.html`)

#### New Panel: "Incomplete Documents"

Features:

- **Summary Badge**: Shows total count of unique incomplete documents
- **Four Metric Cards**: One for each missing data type
  - Missing Summary
  - Missing Extracted Text
  - Missing Keywords
  - Missing Embeddings
- **Expandable Detail Sections**: Click "View Details" to see document lists
- **Document Cards**: Show ID, filename, status, dates, and errors

#### Visual Design

- Color-coded status badges (COMPLETED, FAILED, PENDING, PROCESSING)
- Red badge for incomplete count (turns green when zero)
- Responsive grid layout
- Scrollable lists (max 400px height)
- Error messages displayed inline

## API Response Structure

```json
{
  "summary": {
    "count": 5,
    "documents": [
      {
        "id": 123,
        "filename": "document.pdf",
        "status": "COMPLETED",
        "created_at": "2025-12-02T19:17:52",
        "processed_at": "2025-12-02T19:18:00",
        "processing_error": "Error code: 429 - insufficient_quota"
      }
    ]
  },
  "extracted_text": { ... },
  "keywords": { ... },
  "embeddings": { ... },
  "total_unique_incomplete": 8
}
```

## Database Queries

The feature uses sophisticated SQL queries to identify missing data:

- **Missing Summary**: Checks `ai_analysis` JSONB field for null or missing summary
- **Missing Text**: Checks `extracted_text` field for null or empty
- **Missing Keywords**: Checks `keywords` JSONB field for null or missing keywords array
- **Missing Embeddings**: Checks `search_vector` field for null

## Usage

1. Navigate to `/admin/dashboard`
2. Scroll to the "Incomplete Documents" panel
3. View summary counts for each missing data type
4. Click "View Details" on any metric card to see specific documents
5. Use document IDs to identify which documents need reprocessing

## Reprocessing Documents

Once API quota is restored, incomplete documents can be reprocessed using:

```
POST /api/documents/{document_id}/reprocess
```

## Performance Considerations

- Each category limited to 100 documents to prevent performance issues
- Queries use existing database indexes
- Results are not cached (always shows current state)
- Expandable sections load on-demand

## Future Enhancements

Potential improvements:

1. Batch reprocessing button
2. Filter by date range
3. Export incomplete document list
4. Automatic retry when quota restored
5. Email notifications for failed documents

## Testing

To test the feature:

1. Access the admin dashboard
2. Look for documents uploaded during API quota issues
3. Verify counts match database queries
4. Test expandable sections
5. Verify document details are accurate
