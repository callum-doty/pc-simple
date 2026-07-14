"""
Tests for OCR output cleanup (services/ai_service.py).

Real bug report: extracted_text for a scanned mail piece came back containing
markdown syntax (**bold**, # headers) and a run of ~90 literal `&nbsp;` HTML
entities standing in for a visual whitespace gap in a print-production slug
line. The OCR system prompt said "preserve formatting" without saying *how*,
so the model reached for markdown/HTML. This pollutes full-text search and
gets re-fed into later analysis prompts as noise. Fixed with a tightened
prompt (not covered here — no live model call) plus a defensive sanitizer
that's covered here.
"""

from unittest.mock import AsyncMock, Mock

import pytest

from services.ai_service import AIService


def make_ai_service() -> AIService:
    return AIService(db=Mock())


class TestSanitizeOcrText:
    def test_strips_multiline_bold(self):
        text = "**EVERY YEAR,\nMISSOURI\nPOLITICIANS\nREACH INTO OUR PURSE.**"
        result = AIService._sanitize_ocr_text(text)
        assert "**" not in result
        assert "EVERY YEAR,\nMISSOURI\nPOLITICIANS\nREACH INTO OUR PURSE." == result

    def test_strips_single_asterisk_italics(self):
        result = AIService._sanitize_ocr_text("THAT WERE *ALREADY PAID FOR.*")
        assert result == "THAT WERE ALREADY PAID FOR."

    def test_collapses_repeated_nbsp_entities(self):
        text = "Missouri Promise 03b Grandma.indd 1 " + "&nbsp;" * 90 + " 7/13/26 9:06 PM"
        result = AIService._sanitize_ocr_text(text)
        assert "&nbsp;" not in result
        assert result == "Missouri Promise 03b Grandma.indd 1 7/13/26 9:06 PM"

    def test_strips_atx_headers(self):
        result = AIService._sanitize_ocr_text(
            "# AMENDMENT 5 MAKES POLITICIANS LIVE ON A BUDGET"
        )
        assert result == "AMENDMENT 5 MAKES POLITICIANS LIVE ON A BUDGET"

    def test_decodes_common_html_entities(self):
        result = AIService._sanitize_ocr_text("Smith &amp; Jones for &quot;Missouri&quot;")
        assert result == 'Smith & Jones for "Missouri"'

    def test_does_not_touch_snake_case_filenames(self):
        text = "file_name_with_underscores.docx should stay intact"
        assert AIService._sanitize_ocr_text(text) == text

    def test_strips_genuine_underscore_emphasis(self):
        result = AIService._sanitize_ocr_text("_emphasis word_ at start of a sentence.")
        assert result == "emphasis word at start of a sentence."

    def test_lone_asterisk_is_left_alone(self):
        text = "5 * 3 = 15 is not italic"
        assert AIService._sanitize_ocr_text(text) == text

    def test_handles_empty_and_none(self):
        assert AIService._sanitize_ocr_text("") == ""
        assert AIService._sanitize_ocr_text(None) is None

    def test_preserves_intentional_line_breaks(self):
        text = "Line one.\nLine two.\nLine three."
        assert AIService._sanitize_ocr_text(text) == text


class TestExtractTextFromImageSanitization:
    @pytest.mark.asyncio
    async def test_sanitizes_raw_model_output(self, monkeypatch):
        ai_service = make_ai_service()
        ai_service.ai_provider = "anthropic"  # bypass the "no provider" early return
        dirty = "**Vote Yes**\n\nfile.indd 1 " + "&nbsp;" * 20 + " 9:06 PM"
        monkeypatch.setattr(
            ai_service, "_call_ai_for_raw_text", AsyncMock(return_value=dirty)
        )

        result = await ai_service._extract_text_from_image(b"fake-image-bytes")

        assert "**" not in result
        assert "&nbsp;" not in result
        assert result == "Vote Yes\n\nfile.indd 1 9:06 PM"
