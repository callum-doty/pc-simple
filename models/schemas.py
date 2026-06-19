"""
Typed Pydantic schemas for Document JSONB fields.

Applied in the Python layer — the DB columns remain JSONB for flexibility.
Every schema carries a ``schema_version`` field so that migration code can detect
and upgrade documents produced by older versions of the pipeline.

Schema versioning contract:
  - Increment ``schema_version`` when the field shape changes in a breaking way.
  - Add a branch to the ``upgrade()`` classmethod to normalise old documents.
  - ``from_raw()`` always tolerates missing keys (returns defaults).
  - ``to_storage()`` is the canonical serialisation path — always call it on write.

See docs/architecture-fixes/FIX-007.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# KeywordMapping — embedded inside AIAnalysis and KeywordsData
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
    # Present in documents processed before schema_version=1 — kept for
    # backward-compatible reads, not written in new documents.
    document_analysis: Optional[Dict[str, Any]] = None

    def get_summary(self) -> Optional[str]:
        """Return summary regardless of which schema version the data is in."""
        if self.summary:
            return self.summary
        if self.document_analysis and isinstance(self.document_analysis, dict):
            return self.document_analysis.get("summary")
        return None

    @classmethod
    def from_raw(cls, raw: Optional[Dict[str, Any]]) -> "AIAnalysis":
        """
        Deserialise from a raw JSONB dict, tolerating missing or extra keys.
        Returns an empty AIAnalysis if raw is None or malformed.
        """
        if not raw or not isinstance(raw, dict):
            return cls()
        try:
            upgraded = cls.upgrade(dict(raw))
            return cls.model_validate(upgraded)
        except Exception:
            # Fallback: preserve whatever summary we can find
            summary = raw.get("summary")
            if not summary and isinstance(raw.get("document_analysis"), dict):
                summary = raw["document_analysis"].get("summary")
            return cls(summary=summary, document_analysis=raw.get("document_analysis"))

    def to_storage(self) -> Dict[str, Any]:
        """Serialise for storage, always including schema_version."""
        return self.model_dump(exclude_none=False)

    @classmethod
    def upgrade(cls, raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        Upgrade a raw dict from an older schema version to the current one.
        Add a new branch here whenever schema_version increments.
        """
        version = raw.get("schema_version", 0)
        if version < 1:
            # Pre-versioning: normalise nested document_analysis.summary to top-level.
            if "document_analysis" in raw and not raw.get("summary"):
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
    def from_raw(cls, raw: Optional[Dict[str, Any]]) -> "KeywordsData":
        if not raw or not isinstance(raw, dict):
            return cls()
        try:
            return cls.model_validate(raw)
        except Exception:
            return cls(
                keywords=raw.get("keywords", []),
                categories=raw.get("categories", []),
            )

    def to_storage(self) -> Dict[str, Any]:
        return self.model_dump(exclude_none=False)


# ---------------------------------------------------------------------------
# ProcessingCost — sub-schema within FileMetadata (added by FIX-002)
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
    # Page index of the last successfully processed page; used for retry
    # checkpointing (FIX-002). Cleared on task completion or reprocess reset.
    processing_checkpoint: Optional[int] = None
    feature_extraction: Optional[FeatureExtractionMeta] = None

    # Extra keys (e.g. legacy fields, forward-compatible additions) are passed
    # through transparently so old documents never fail deserialisation.
    model_config = {"extra": "allow"}

    @classmethod
    def from_raw(cls, raw: Optional[Dict[str, Any]]) -> "FileMetadata":
        if not raw or not isinstance(raw, dict):
            return cls()
        try:
            return cls.model_validate(raw)
        except Exception:
            return cls()

    def to_storage(self) -> Dict[str, Any]:
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
    def from_raw(cls, raw: Optional[Dict[str, Any]]) -> "EmbeddingProvenance":
        if not raw or not isinstance(raw, dict):
            return cls()
        try:
            return cls.model_validate(raw)
        except Exception:
            return cls()

    def to_storage(self) -> Dict[str, Any]:
        return self.model_dump(exclude_none=True)
