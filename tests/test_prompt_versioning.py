"""
Tests for prompt version tracking (PromptManager.PROMPT_VERSION and
AIService.OCR_PROMPT_VERSION).

Why this exists: prompt wording changes (like the OCR plain-text fix in this
same change set) can silently shift what gets extracted from a document
without changing the JSONB *shape* at all — so schema_version alone can't
flag it. Every document processed going forward should record which prompt
version produced its ai_analysis and, if OCR was used, its extracted_text,
so a future data-quality investigation can correlate a shift in the data with
a known prompt change instead of chasing an unexplained trend. Documents
processed before this change simply won't have these fields — that's
intentional (see models/schemas.py) rather than a bug.
"""

from unittest.mock import AsyncMock, Mock

import pytest

from models.schemas import AIAnalysis, FileMetadata
from services.ai_service import AIService
from services.prompt_manager import PromptManager


def make_ai_service() -> AIService:
    return AIService(db=Mock())


class TestSchemaDefaults:
    def test_ai_analysis_prompt_version_defaults_to_none_not_a_fake_version(self):
        # A raw dict with no prompt_version key (i.e. every document processed
        # before this change) must read back as unknown, not as "version 1".
        analysis = AIAnalysis.from_raw({"summary": "old document, no version tag"})
        assert analysis.prompt_version is None

    def test_file_metadata_ocr_prompt_version_defaults_to_none(self):
        meta = FileMetadata.from_raw({"page_count": 3})
        assert meta.ocr_prompt_version is None


class TestAnalysisStampsPromptVersion:
    @pytest.mark.asyncio
    async def test_unified_analysis_stamps_current_prompt_version(self, monkeypatch):
        ai_service = make_ai_service()
        ai_service.anthropic_client = Mock()  # bypass "not configured" fallback
        monkeypatch.setattr(
            ai_service,
            "_call_anthropic_api_with_system",
            AsyncMock(return_value={"summary": "a real analysis"}),
        )

        result = await ai_service._perform_unified_analysis(
            "some extracted text", b"fake", "pdf", "doc.pdf"
        )

        assert result["prompt_version"] == PromptManager.PROMPT_VERSION

    @pytest.mark.asyncio
    async def test_unified_analysis_does_not_stamp_version_on_api_error(self, monkeypatch):
        ai_service = make_ai_service()
        ai_service.anthropic_client = Mock()
        monkeypatch.setattr(
            ai_service,
            "_call_anthropic_api_with_system",
            AsyncMock(return_value={"error": "rate limited"}),
        )

        result = await ai_service._perform_unified_analysis(
            "some extracted text", b"fake", "pdf", "doc.pdf"
        )

        assert "prompt_version" not in result

    @pytest.mark.asyncio
    async def test_modular_analysis_stamps_current_prompt_version(self, monkeypatch):
        ai_service = make_ai_service()
        monkeypatch.setattr(
            ai_service,
            "_run_analysis_prompt",
            AsyncMock(return_value={"ok": True}),
        )
        monkeypatch.setattr(
            ai_service.prompt_manager, "get_taxonomy_keyword_prompt", AsyncMock(return_value={})
        )

        result = await ai_service._perform_modular_analysis(
            "text", b"fake", "text", "doc.txt"
        )

        assert result["prompt_version"] == PromptManager.PROMPT_VERSION

    @pytest.mark.asyncio
    async def test_modular_analysis_awaits_taxonomy_keyword_prompt(self, monkeypatch):
        """
        Regression test: get_taxonomy_keyword_prompt is `async def`, but Step 6
        of _perform_modular_analysis previously passed the coroutine object
        straight to _run_analysis_prompt without awaiting it first. That made
        `prompt_data["user"]` blow up with a TypeError inside _run_analysis_prompt,
        which the enclosing try/except swallowed into a bare {"error": ...},
        discarding steps 1-5 (metadata/classification/entities/text/design)
        every time modular analysis actually ran. If this regresses, this
        test fails with a KeyError instead of finding "taxonomy_keywords".
        """
        ai_service = make_ai_service()
        monkeypatch.setattr(
            ai_service,
            "_run_analysis_prompt",
            AsyncMock(return_value={"ok": True}),
        )
        monkeypatch.setattr(
            ai_service.prompt_manager,
            "get_taxonomy_keyword_prompt",
            AsyncMock(return_value={"system": "s", "user": "u"}),
        )

        result = await ai_service._perform_modular_analysis(
            "text", b"fake", "text", "doc.txt"
        )

        assert "error" not in result
        assert result["taxonomy_keywords"] == {"ok": True}


class TestAnalyzeDocumentStampsOcrVersion:
    @pytest.mark.asyncio
    async def test_pdf_gets_ocr_prompt_version(self, monkeypatch):
        ai_service = make_ai_service()
        ai_service.storage_service.get_file = AsyncMock(return_value=b"%PDF-fake")
        monkeypatch.setattr(ai_service, "_extract_text", AsyncMock(return_value="page text"))
        monkeypatch.setattr(
            ai_service, "_perform_unified_analysis", AsyncMock(return_value={"summary": "ok"})
        )

        result = await ai_service.analyze_document("/fake/a.pdf", "a.pdf")

        assert result["ocr_prompt_version"] == AIService.OCR_PROMPT_VERSION

    @pytest.mark.asyncio
    async def test_docx_gets_no_ocr_prompt_version(self, monkeypatch):
        ai_service = make_ai_service()
        ai_service.storage_service.get_file = AsyncMock(return_value=b"content")
        monkeypatch.setattr(ai_service, "_extract_text", AsyncMock(return_value="docx text"))
        monkeypatch.setattr(
            ai_service, "_perform_unified_analysis", AsyncMock(return_value={"summary": "ok"})
        )

        result = await ai_service.analyze_document("/fake/a.docx", "a.docx")

        assert result["ocr_prompt_version"] is None
