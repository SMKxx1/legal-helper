"""Tests for app.ingestion.ocr (the scanned-PDF fallback).

OCR was at 0% coverage. The actual recognition engines (tesseract/apple/paddle) are not guaranteed in
CI, so the *engine* round-trip is skipped when no backend is installed — but the deterministic, backend-
free logic that decides WHICH backend to run, WHETHER a page is garbled, and how to SCORE OCR output is
tested unconditionally (it runs in CI and is what guards the escalate-on-bad-page behavior).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.ingestion import ocr

# --------------------------------------------------------------------------- #
# Output-quality scoring (no backend needed)
# --------------------------------------------------------------------------- #


def test_text_quality_empty_is_zero():
    assert ocr.text_quality("") == (0.0, 0)
    assert ocr.text_quality("   \n  ") == (0.0, 0)


def test_text_quality_clean_text_scores_high():
    score, n = ocr.text_quality(
        "The Receiving Party shall keep Confidential Information secret"
    )
    assert n == 8  # alphabetic tokens of length >= 2
    assert score > 0.8


def test_text_quality_gibberish_scores_low():
    score, _ = ocr.text_quality("xkqz vbwfgh zzztpq qntx")
    assert score < 0.5


def test_structural_word_like_distinguishes_words_from_noise():
    assert ocr._structural_word_like("secret") is True
    assert ocr._structural_word_like("confidential") is True
    assert ocr._structural_word_like("xkcdwq") is False  # no vowel
    assert ocr._structural_word_like("abcdfgh") is False  # 6-consonant run


def test_page_failed_only_flags_token_rich_garbled_pages():
    # Token-rich + garbled -> failed (re-OCR candidate).
    assert ocr._page_failed(" ".join(["xkqz"] * 30)) is True
    # Token-rich + clean -> not failed.
    assert ocr._page_failed(" ".join(["secret"] * 30)) is False
    # Sparse page (few tokens) -> never judged, even if garbled.
    assert ocr._page_failed("xkqz vbwgh") is False


# --------------------------------------------------------------------------- #
# Backend resolution (probes monkeypatched -> deterministic, OS-independent)
# --------------------------------------------------------------------------- #


def _stub_backends(monkeypatch, *, tess=False, apple=False, paddle=False):
    monkeypatch.setattr(ocr, "_tesseract_available", lambda: tess)
    monkeypatch.setattr(ocr, "_apple_available", lambda: apple)
    monkeypatch.setattr(ocr, "_paddle_available", lambda: paddle)


def test_resolve_backend_explicit_choice(monkeypatch):
    monkeypatch.setattr(settings, "ocr_backend", "tesseract")
    _stub_backends(monkeypatch, tess=True)
    assert ocr._resolve_backend() == "tesseract"
    _stub_backends(monkeypatch, tess=False)
    assert ocr._resolve_backend() is None


def test_resolve_backend_auto_prefers_apple_then_tesseract(monkeypatch):
    monkeypatch.setattr(settings, "ocr_backend", "auto")
    _stub_backends(monkeypatch, apple=True, tess=True)
    assert ocr._resolve_backend() == "apple"
    _stub_backends(monkeypatch, apple=False, tess=True)
    assert ocr._resolve_backend() == "tesseract"
    # auto never auto-selects the heavy/slow paddle backend.
    _stub_backends(monkeypatch, apple=False, tess=False, paddle=True)
    assert ocr._resolve_backend() is None


def test_ocr_available_honors_enabled_flag(monkeypatch):
    monkeypatch.setattr(settings, "ocr_backend", "tesseract")
    _stub_backends(monkeypatch, tess=True)
    monkeypatch.setattr(settings, "ocr_enabled", True)
    assert ocr.ocr_available() is True
    monkeypatch.setattr(settings, "ocr_enabled", False)
    assert ocr.ocr_available() is False  # disabled wins over an available backend


def test_backend_available_unknown_name_is_false():
    assert ocr._backend_available("nonsense") is False


def test_resolve_fallback_rules(monkeypatch):
    # Escalation only applies when the primary is the weak tesseract backend.
    monkeypatch.setattr(settings, "ocr_escalate", True)
    monkeypatch.setattr(settings, "ocr_fallback_backend", "")
    _stub_backends(monkeypatch, apple=True)
    assert ocr._resolve_fallback("tesseract") == "apple"
    assert ocr._resolve_fallback("apple") is None  # primary not tesseract

    # Auto-prefers paddle when apple is unavailable.
    _stub_backends(monkeypatch, apple=False, paddle=True)
    assert ocr._resolve_fallback("tesseract") == "paddle"

    # Explicit fallback wins when available; escalation off -> None.
    monkeypatch.setattr(settings, "ocr_fallback_backend", "paddle")
    _stub_backends(monkeypatch, paddle=True)
    assert ocr._resolve_fallback("tesseract") == "paddle"
    monkeypatch.setattr(settings, "ocr_escalate", False)
    assert ocr._resolve_fallback("tesseract") is None


def test_tess_config_includes_tessdata_dir_override(monkeypatch):
    monkeypatch.setattr(settings, "ocr_tessdata_dir", "")
    assert ocr._tess_config() == "--oem 1 --psm 3"
    monkeypatch.setattr(settings, "ocr_tessdata_dir", "/opt/tessdata_best")
    assert '--tessdata-dir "/opt/tessdata_best"' in ocr._tess_config()


# --------------------------------------------------------------------------- #
# ocr_pdf_pages guard branches (no backend needed)
# --------------------------------------------------------------------------- #


def test_ocr_pdf_pages_returns_empty_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ocr_enabled", False)
    assert ocr.ocr_pdf_pages(tmp_path / "whatever.pdf") == []


def test_ocr_pdf_pages_returns_empty_when_no_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ocr_enabled", True)
    monkeypatch.setattr(ocr, "_resolve_backend", lambda: None)
    assert ocr.ocr_pdf_pages(tmp_path / "whatever.pdf") == []


# --------------------------------------------------------------------------- #
# Real OCR round-trip — only when a backend is actually installed.
# --------------------------------------------------------------------------- #


def _build_image_only_pdf(path: Path, text: str) -> None:
    """A PDF with NO text layer: render text to a raster page then wrap the image, so the only way to
    recover the text is OCR."""
    import fitz

    src = fitz.open()
    page = src.new_page()
    page.insert_text((72, 144), text, fontsize=28)
    pix = page.get_pixmap(matrix=fitz.Matrix(200 / 72, 200 / 72))

    out = fitz.open()
    img_page = out.new_page(width=pix.width, height=pix.height)
    img_page.insert_image(img_page.rect, pixmap=pix)
    out.save(str(path))
    out.close()
    src.close()


@pytest.mark.skipif(not ocr.ocr_available(), reason="no OCR backend installed")
def test_ocr_pdf_text_recovers_text_from_image_only_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ocr_enabled", True)
    path = tmp_path / "scan.pdf"
    _build_image_only_pdf(path, "CONFIDENTIAL NONDISCLOSURE")

    recovered = ocr.ocr_pdf_text(path).upper()

    assert "CONFIDENTIAL" in recovered
