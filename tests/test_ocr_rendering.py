"""
Tests for the OCR render budget.

A fixed DPI bounds nothing when page sizes vary. This corpus runs from
letter-size mailers to large-format posters, and at the previous 200 DPI a
24x36" page rasterised to 4800x7200 — a ~104MB pixmap, held alongside its PNG
and base64 copies. That single-page allocation was the OOM behind the worker's
restart loop, which is why halving --concurrency made it *worse* rather than
better: one oversized document exceeds the container on its own, so the crash
rate tracks which documents are in flight, not how many.

Nothing is lost by capping: the vision API downscales any image past the
model's long-edge limit server-side, so those pixels never reached the model.
"""

import fitz
import pytest

from config import get_settings
from services.ai_service import AIService

BUDGET = get_settings().ocr_max_image_edge_px


@pytest.fixture
def service():
    # The render helper is pure geometry — no DB session or API client needed.
    return AIService.__new__(AIService)


def page_of(width_in, height_in):
    doc = fitz.open()
    doc.new_page(width=width_in * 72, height=height_in * 72)
    return doc, doc[0]


def rendered_size(service, width_in, height_in):
    doc, page = page_of(width_in, height_in)
    try:
        m = service._ocr_render_matrix(page)
        return round(width_in * 72 * m.a), round(height_in * 72 * m.d)
    finally:
        doc.close()


@pytest.mark.parametrize(
    "width_in,height_in",
    [(8.5, 11), (11, 17), (24, 36), (36, 48), (48, 36)],
)
def test_long_edge_never_exceeds_the_budget(service, width_in, height_in):
    w, h = rendered_size(service, width_in, height_in)
    assert max(w, h) <= BUDGET, f"{width_in}x{height_in}in rendered to {w}x{h}"


def test_oversized_pages_are_brought_down_dramatically(service):
    # The case that was killing the worker: 24x36" at 200 DPI is ~104MB of RGB.
    w, h = rendered_size(service, 24, 36)
    old_bytes = (24 * 200) * (36 * 200) * 3
    new_bytes = w * h * 3
    assert new_bytes < old_bytes / 10


def test_small_pages_are_never_upscaled_past_the_previous_dpi(service):
    # A business-card-sized page must not be blown up to fill the budget just
    # because it can be — that would spend more memory than before, not less.
    w, h = rendered_size(service, 2, 3.5)
    assert w <= 2 * 200 and h <= 3.5 * 200


def test_degenerate_page_does_not_divide_by_zero(service):
    """
    A malformed PDF can report a zero-area page. Stubbed rather than built with
    new_page(), which silently substitutes a letter-size page for a 0x0 request
    and so cannot reproduce the case the guard exists for.
    """

    class ZeroRect:
        width = 0
        height = 0

    class ZeroPage:
        rect = ZeroRect()

    m = service._ocr_render_matrix(ZeroPage())
    assert m.a == 1.0 and m.d == 1.0
