# FIX-007: Typed Pydantic Schemas for JSONB Fields

**Priority:** P1  
**Effort:** ~1 day  
**Files affected:** new `models/schemas.py`, `models/document.py`, `services/ai_service.py`, `worker.py`

---

## Problem

The `Document` model has four JSONB fields with no enforced structure:

| Field | Line | What it currently contains |
|-------|------|---------------------------|
| `ai_analysis` | 60 | Dict with `summary`, `document_analysis`, `keyword_mappings`, `page_count` — but shape varies by provider and document type |
| `keywords` | 61 | Dict with `keywords`, `categories`, `keyword_mappings`, `mapping_count`, `extraction_timestamp` |
| `file_metadata` | 62 | Catch-all: page count, file info, feature extraction trace, processing cost (after FIX-002), processing checkpoint (after FIX-002) |
| `embedding_provenance` | 69 | Dict with embedding origin metadata |

The consequence is observable in the codebase today:

1. `Document.get_summary()` (line 110) checks two different key paths because the
   `ai_analysis` shape changed at some point and both old and new shapes exist in
   production data.

2. `worker.py:81–88` navigates `chunk_analysis` with multiple `.get()` fallbacks for
   the same reason.

3. `Document._update_search_content()` (line 163) accesses `ai_analysis` keys
   defensively with `.get()` throughout.

4. The `file_metadata` field now also carries processing cost (FIX-002) and processing
   checkpoint (FIX-002). Without a schema, two developers will make conflicting
   assumptions about what keys exist.

At current scale this is a maintainability cost. At 10x data volume with multiple
engineers, it becomes a correctness risk: a schema change in `ai_service.py` will
silently produce wrong data for queries that assume the old shape, with no validation
error to surface the incompatibility.

---

## Solution

Define Pydantic models for each JSONB field. Use them:
1. **On write** — validate before storing. Bad data is rejected at the source.
2. **On read** — deserialize into typed objects. Access is dot-notation, not `.get()`.
3. **For versioning** — each schema has a `schema_version` field. When the shape
   changes, increment the version. Migration code can detect and upgrade old documents.

The JSONB columns themselves do not change. Pydantic models are applied in the Python
layer only — no schema migration required for this fix.

---

## Implementation Steps

### Step 1 — Create `models/schemas.py`

Create a new file `models/schemas.py`:

```python
"""
Typed Pydantic schemas for Document JSONB fields.
These are applied in the Python layer — the DB columns remain JSONB.

Schema versioning:
  When a field shape changes, increment schema_version in the relevant class
  and add a migration path in the `upgrade` classmethod.
"""

from __future__ import annotations
from pydantic import BaseModel, Field, validator
from typing import Optional, List, Dict, Any
from datetime import datetime


# ---------------------------------------------------------------------------
# KeywordMapping — used inside both AIAnalysis and KeywordsData
# ---------------------------------------------------------------------------

class KeywordMapping(BaseModel):
    verbatim_term: str
    mapped_canonical_term: str
    primary_category: Optional[str] = None
    subcategory: Optional[str] = None
    confidence: Optional[str] = None


# ---------------------------------------------------------------------------
# AIAnalysis — stored in Document.ai_analysis
# ---------------------------------------------------------------------------

class AIAnalysis(BaseModel):
    schema_version: int = 1
    summary: Optional[str] = None
    page_count: Optional[int] = None
    analysis_type: Optional[str] = None
    keyword_mappings: List[KeywordMapping] = Field(default_factory=list)
    # Legacy key — present in documents processed before schema_version=1
    document_analysis: Optional[Dict[str, Any]] = None

    def get_summary(self) -> Optional[str]:
        """
        Return summary regardless of which schema version the data is in.
        Handles the old nested `document_analysis.summary` path.
        """
        if self.summary:
            return self.summary
        if self.document_analysis and isinstance(self.document_analysis, dict):
            return self.document_analysis.get("summary")
        return None

    @classmethod
    def from_raw(cls, raw: dict | None) -> "AIAnalysis":
        """
        Deserialize from a raw JSONB dict, tolerating missing or extra keys.
        Returns an empty AIAnalysis if raw is None or malformed.
        """
        if not raw or not isinstance(raw, dict):
            return cls()
        try:
            return cls.model_validate(raw)
        except Exception:
            # Fallback for completely unexpected shapes
            return cls(summary=raw.get("summary"), document_analysis=raw)

    def to_storage(self) -> dict:
        """Serialize for storage, always including schema_version."""
        return self.model_dump(exclude_none=False)

    @classmethod
    def upgrade(cls, raw: dict) -> dict:
        """
        Upgrade a raw dict from an older schema version to the current one.
        Add a new branch here whenever schema_version increments.
        """
        version = raw.get("schema_version", 0)
        if version == 0:
            # Pre-versioning: normalize nested document_analysis.summary to top-level
            if "document_analysis" in raw and "summary" not in raw:
                doc_analysis = raw.get("document_analysis", {})
                if isinstance(doc_analysis, dict):
                    raw["summary"] = doc_analysis.get("summary")
            raw["schema_version"] = 1
        return raw


# ---------------------------------------------------------------------------
# KeywordsData — stored in Document.keywords
# ---------------------------------------------------------------------------

class KeywordsData(BaseModel):
    schema_version: int = 1
    keywords: List[str] = Field(default_factory=list)
    categories: List[str] = Field(default_factory=list)
    keyword_mappings: List[KeywordMapping] = Field(default_factory=list)
    mapping_count: int = 0
    extraction_timestamp: Optional[str] = None

    @classmethod
    def from_raw(cls, raw: dict | None) -> "KeywordsData":
        if not raw or not isinstance(raw, dict):
            return cls()
        try:
            return cls.model_validate(raw)
        except Exception:
            return cls(keywords=raw.get("keywords", []))

    def to_storage(self) -> dict:
        return self.model_dump(exclude_none=False)


# ---------------------------------------------------------------------------
# ProcessingCost — sub-schema within FileMetadata
# ---------------------------------------------------------------------------

class ProcessingCost(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    provider: Optional[str] = None
    processed_at: Optional[str] = None


# ---------------------------------------------------------------------------
# FeatureExtractionMeta — sub-schema within FileMetadata
# ---------------------------------------------------------------------------

class FeatureExtractionMeta(BaseModel):
    canonical_source: Optional[str] = None
    date_source: Optional[str] = None
    state_source: Optional[str] = None


# ---------------------------------------------------------------------------
# FileMetadata — stored in Document.file_metadata
# ---------------------------------------------------------------------------

class FileMetadata(BaseModel):
    schema_version: int = 1
    page_count: Optional[int] = None
    file_type: Optional[str] = None
    processing_cost: Optional[ProcessingCost] = None
    processing_checkpoint: Optional[int] = None
    feature_extraction: Optional[FeatureExtractionMeta] = None
    # Allow extra keys for forward compatibility
    model_config = {"extra": "allow"}

    @classmethod
    def from_raw(cls, raw: dict | None) -> "FileMetadata":
        if not raw or not isinstance(raw, dict):
            return cls()
        try:
            return cls.model_validate(raw)
        except Exception:
            return cls()

    def to_storage(self) -> dict:
        return self.model_dump(exclude_none=True)


# ---------------------------------------------------------------------------
# EmbeddingProvenance — stored in Document.embedding_provenance
# ---------------------------------------------------------------------------

class EmbeddingProvenance(BaseModel):
    schema_version: int = 1
    model: Optional[str] = None
    version: Optional[int] = None
    text_components: Optional[List[str]] = None
    generated_at: Optional[str] = None

    @classmethod
    def from_raw(cls, raw: dict | None) -> "EmbeddingProvenance":
        if not raw or not isinstance(raw, dict):
            return cls()
        try:
            return cls.model_validate(raw)
        except Exception:
            return cls()

    def to_storage(self) -> dict:
        return self.model_dump(exclude_none=True)
```

---

### Step 2 — Add typed accessor methods to `Document`

In `models/document.py`, add imports and typed accessors alongside the existing
raw `.get()` methods. Do not remove the existing methods yet — existing callers
should migrate incrementally.

```python
# At top of models/document.py
from models.schemas import AIAnalysis, KeywordsData, FileMetadata, EmbeddingProvenance

class Document(Base):
    # ... existing columns ...

    # --- Typed accessors (use these in new code) ---

    def get_ai_analysis(self) -> AIAnalysis:
        """Return ai_analysis as a typed object. Handles all schema versions."""
        return AIAnalysis.from_raw(self.ai_analysis)

    def get_keywords_data(self) -> KeywordsData:
        """Return keywords as a typed object."""
        return KeywordsData.from_raw(self.keywords)

    def get_file_metadata(self) -> FileMetadata:
        """Return file_metadata as a typed object."""
        return FileMetadata.from_raw(self.file_metadata)

    def get_embedding_provenance(self) -> EmbeddingProvenance:
        """Return embedding_provenance as a typed object."""
        return EmbeddingProvenance.from_raw(self.embedding_provenance)

    def set_ai_analysis(self, analysis: AIAnalysis) -> None:
        """Validate and store ai_analysis."""
        self.ai_analysis = analysis.to_storage()

    def set_keywords_data(self, data: KeywordsData) -> None:
        """Validate and store keywords."""
        self.keywords = data.to_storage()
        self._update_search_content()

    def set_file_metadata(self, meta: FileMetadata) -> None:
        """Validate and store file_metadata."""
        self.file_metadata = meta.to_storage()

    # --- Rewrite get_summary() to use the typed accessor ---
    def get_summary(self) -> Optional[str]:
        return self.get_ai_analysis().get_summary()
```

---

### Step 3 — Update `worker.py` to write via typed schemas

In `worker.py`, `_process_pdf_document_by_page` currently assembles `final_ai_analysis`
as a raw dict (lines 105–110). Replace with:

```python
from models.schemas import AIAnalysis, KeywordMapping

final_ai_analysis = AIAnalysis(
    summary=final_summary,
    page_count=total_pages,
    analysis_type="chunked_unified",
    keyword_mappings=[
        KeywordMapping(**m) for m in final_mappings if isinstance(m, dict)
    ],
)

document_service.update_document_content_sync(
    document_id,
    extracted_text=final_extracted_text,
    ai_analysis=final_ai_analysis.to_storage(),  # validated before writing
    # ...
)
```

---

### Step 4 — Update `DocumentService.update_document_content_sync`

In `services/document_service.py`, validate the `ai_analysis` parameter if it arrives
as a raw dict:

```python
from models.schemas import AIAnalysis

def update_document_content_sync(self, document_id: int, ai_analysis: dict | None = None, ...):
    # Validate on write
    if ai_analysis is not None:
        validated = AIAnalysis.from_raw(ai_analysis)
        ai_analysis = validated.to_storage()
    # ... rest of existing update logic ...
```

This adds validation at the service boundary without requiring all callers to be
updated simultaneously.

---

### Step 5 — Backfill existing documents (optional, non-urgent)

Once `AIAnalysis.upgrade()` is implemented, a backfill script can normalize old
documents:

```python
# backfill_schema_versions.py
from models.schemas import AIAnalysis
from database import get_db
from models.document import Document

db = next(get_db())
for doc in db.query(Document).filter(Document.ai_analysis.isnot(None)):
    raw = doc.ai_analysis
    version = raw.get("schema_version", 0) if isinstance(raw, dict) else 0
    if version < AIAnalysis.schema_version:
        upgraded = AIAnalysis.upgrade(raw)
        doc.ai_analysis = upgraded
db.commit()
```

Run this as a one-off operation after deployment. It is non-destructive — it only
adds `schema_version` and normalizes existing keys.

---

## Acceptance Criteria

- [ ] `models/schemas.py` exists with `AIAnalysis`, `KeywordsData`, `FileMetadata`,
      and `EmbeddingProvenance`.
- [ ] Each schema has `schema_version`, `from_raw()`, and `to_storage()` methods.
- [ ] `Document.get_summary()` uses `AIAnalysis.get_summary()` and handles both
      old and new shapes without a code path in `get_summary` itself.
- [ ] Writing to `ai_analysis` via `set_ai_analysis()` validates the data and raises
      a `ValidationError` if it does not conform to the schema.
- [ ] New worker code writes AI analysis via `AIAnalysis(...)` rather than raw dicts.
- [ ] Existing documents with old JSONB shapes still deserialize correctly via
      `from_raw()`.
- [ ] All `.get()` chains in `Document` methods that access `ai_analysis` or `keywords`
      are replaced with typed accessor calls.

---

## Notes

**Do not remove the raw JSONB columns.** Pydantic validation happens in the Python
layer. The PostgreSQL columns remain JSONB — this is intentional for flexibility
and for direct SQL queries.

**`extra = "allow"` in `FileMetadata`.** This is intentional. `file_metadata` is a
catch-all for operational state (checkpoints, cost, feature extraction traces). New
keys should be added to the schema when they become stable, but `extra = "allow"`
prevents validation failures when encountering forward-compatible unknown keys.

**Do not add `extra = "allow"` to `AIAnalysis` or `KeywordsData`.** These are
core data fields where unexpected keys indicate a schema mismatch that should be
surfaced, not silently ignored.
