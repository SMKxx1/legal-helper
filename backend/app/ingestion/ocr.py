"""Fully-local OCR for scanned / image-only PDFs — zero external egress.

Used as a fallback by :func:`app.ingestion.parser.extract_pdf` when a PDF has no
extractable text layer. Nothing ever leaves the machine.

Cross-platform by design:

* **Tesseract** is the portable default and runs on Linux, Windows and macOS. It
  is bundled into the deployment Docker image (Linux), so the engine behaves
  identically regardless of the host OS. Each page is rendered to a grayscale
  image with PyMuPDF at ``ocr_dpi`` and passed to ``tesseract --oem 1 --psm 3``.
* **Apple Vision** (via the optional, macOS-only ``ocrmac`` package) is used
  automatically when the backend is ``auto`` and it is importable. On real
  scanned contracts it is both faster and slightly more accurate than Tesseract,
  especially on skewed / low-contrast pages. It is never required and is not part
  of the Linux image.
* **PaddleOCR** (PP-OCRv6, Apache-2.0) is an *opt-in* portable backend
  (``OCR_BACKEND=paddle``). It is the most accurate *cross-platform* engine on
  degraded scans (benchmarked F1 0.94 vs Tesseract 0.67 on the noisy FUNSD set;
  ties Apple Vision on clean NDAs) but is heavy (the ``paddlepaddle`` wheel is
  large) and slow on CPU (~25x Tesseract: tens of seconds per page). Never
  selected automatically; enable it explicitly when accuracy on poor scans
  matters more than latency and the extra dependency is acceptable.

Backend selection is controlled by ``settings.ocr_backend`` (``auto`` |
``tesseract`` | ``apple`` | ``paddle``). ``auto`` prefers Apple Vision on macOS
then Tesseract; it never picks Paddle (opt-in only). Benchmarked on the signed-NDA
corpus: heavy preprocessing (binarization/denoise) gave no benefit on these clean
scans and was deliberately left out.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Backend availability
# --------------------------------------------------------------------------- #
def _tesseract_available() -> bool:
    try:
        import pytesseract  # noqa: F401
    except Exception:
        return False
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        logger.warning(
            "pytesseract is installed but the `tesseract` binary was not found"
        )
        return False


def _apple_available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        from ocrmac import ocrmac  # noqa: F401

        return True
    except Exception:
        return False


def _paddle_available() -> bool:
    try:
        import paddleocr  # noqa: F401

        return True
    except Exception:
        return False


def _backend_available(name: str) -> bool:
    return {
        "tesseract": _tesseract_available,
        "apple": _apple_available,
        "paddle": _paddle_available,
    }.get(name, lambda: False)()


def _resolve_fallback(primary: str) -> str | None:
    """Pick the escalation backend used when `primary` produces a bad page.

    Only Tesseract escalates (it is the weak backend). Explicit
    ``ocr_fallback_backend`` wins; otherwise auto-prefer Apple Vision (fast) then
    PaddleOCR (portable). Returns None when escalation is off or no stronger
    backend is installed.
    """
    if not settings.ocr_escalate or primary != "tesseract":
        return None
    explicit = (settings.ocr_fallback_backend or "").lower().strip()
    if explicit:
        return (
            explicit if (explicit != primary and _backend_available(explicit)) else None
        )
    if _apple_available():
        return "apple"
    if _paddle_available():
        return "paddle"
    return None


def _resolve_backend() -> str | None:
    """Return the OCR backend to use, or ``None`` if none is available."""
    choice = (settings.ocr_backend or "auto").lower()
    if choice == "tesseract":
        return "tesseract" if _tesseract_available() else None
    if choice == "apple":
        return "apple" if _apple_available() else None
    if choice == "paddle":
        return "paddle" if _paddle_available() else None
    # auto: prefer Apple Vision on macOS, else Tesseract. Never auto-selects the
    # heavy/slow Paddle backend — it is opt-in via OCR_BACKEND=paddle.
    if _apple_available():
        return "apple"
    if _tesseract_available():
        return "tesseract"
    return None


def ocr_available() -> bool:
    """True when OCR is enabled and at least one backend can run."""
    return bool(settings.ocr_enabled) and _resolve_backend() is not None


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _render_gray_png(page, dpi: int) -> bytes:
    """Render a PyMuPDF page to grayscale PNG bytes (no extra deps)."""
    import fitz  # PyMuPDF

    pix = page.get_pixmap(
        matrix=fitz.Matrix(dpi / 72, dpi / 72), colorspace=fitz.csGRAY
    )
    return pix.tobytes("png")


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #
def _tess_config() -> str:
    cfg = "--oem 1 --psm 3"
    tdir = (settings.ocr_tessdata_dir or "").strip()
    if tdir:
        cfg += f' --tessdata-dir "{tdir}"'
    return cfg


def _ocr_page_tesseract(png: bytes) -> str:
    import io

    import pytesseract
    from PIL import Image

    img = Image.open(io.BytesIO(png))
    return pytesseract.image_to_string(
        img, lang=settings.ocr_lang or "eng", config=_tess_config()
    )


def _ocr_page_apple(png: bytes) -> str:
    import tempfile

    from ocrmac import ocrmac

    with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tf:
        tf.write(png)
        tf.flush()
        results = ocrmac.OCR(
            tf.name,
            framework="vision",
            recognition_level="accurate",
            language_preference=["en-US"],
        ).recognize()
    # results: list of (text, confidence, bbox); preserve reading order as returned.
    return "\n".join(r[0] for r in results)


_PADDLE = None


def _get_paddle():
    """Lazily build and cache the PaddleOCR pipeline (init is expensive)."""
    global _PADDLE
    if _PADDLE is None:
        from paddleocr import PaddleOCR

        _PADDLE = PaddleOCR(
            lang="en",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    return _PADDLE


def _ocr_page_paddle(png: bytes) -> str:
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tf:
        tf.write(png)
        tf.flush()
        res = _get_paddle().predict(tf.name)
    return "\n".join(res[0]["rec_texts"]) if res else ""


_PAGE_FN = {
    "apple": _ocr_page_apple,
    "paddle": _ocr_page_paddle,
    "tesseract": _ocr_page_tesseract,
}

# --------------------------------------------------------------------------- #
# Output-quality scoring (detects garbled / failed OCR — no extra deps)
# --------------------------------------------------------------------------- #
import re as _re  # noqa: E402 — intentionally scoped to this section, below the OCR engine setup

_TOKEN_RE = _re.compile(r"[A-Za-z]{2,}")
_VOWEL_RE = _re.compile(r"[aeiouy]")
_CONS_RUN_RE = _re.compile(r"[bcdfghjklmnpqrstvwxz]{5,}")


def _structural_word_like(tok: str) -> bool:
    """Dependency-free gibberish check: real words have a vowel and no long
    consonant run. Weaker than a dictionary (misses real-looking OCR errors), so
    it is only the fallback when `wordfreq` is unavailable."""
    t = tok.lower()
    if not _VOWEL_RE.search(t):
        return False
    return not _CONS_RUN_RE.search(t)


try:  # `wordfreq` gives a real dictionary signal — the reliable OCR-failure detector.
    from wordfreq import zipf_frequency as _zipf

    def _word_like(tok: str) -> bool:
        return _zipf(tok.lower(), "en") > 0
except Exception:  # pragma: no cover - slim install without wordfreq
    _word_like = _structural_word_like


def text_quality(text: str) -> tuple[float, int]:
    """Return (dictionary-word ratio 0..1, token count) for an OCR result.

    The ratio is the fraction of alphabetic tokens that are real English words.
    Garbled/failed OCR scores low; clean text scores ~0.97+.
    """
    toks = _TOKEN_RE.findall(text or "")
    if not toks:
        return 0.0, 0
    return sum(_word_like(t) for t in toks) / len(toks), len(toks)


def _page_failed(text: str) -> bool:
    """A token-rich page whose words read as gibberish -> Tesseract failed here.

    Sparse pages (few tokens, e.g. a signature page) are NOT judged — too little
    signal, and little to gain from re-OCR.
    """
    score, n = text_quality(text)
    return n >= int(settings.ocr_min_tokens_for_quality or 25) and score < float(
        settings.ocr_min_quality or 0.70
    )


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def ocr_pdf_pages(path: str | Path) -> list[str]:
    """OCR every page of a PDF locally and return per-page text.

    Runs the resolved primary backend. When that backend is Tesseract and a page
    comes back garbled (see :func:`_page_failed`), that single page is re-OCR'd
    with a stronger fallback backend (Apple Vision or PaddleOCR) and the better
    result is kept — so the slow/heavy engine is paid only on the pages that need
    it.

    Returns an empty list when OCR is disabled, no backend is available, or the
    document yields no text. Never raises for an OCR/runtime failure — it logs and
    degrades, so callers can fall back to their own "no text" handling.
    """
    if not settings.ocr_enabled:
        return []
    backend = _resolve_backend()
    if backend is None:
        logger.warning(
            "OCR requested but no backend available (install tesseract or ocrmac)"
        )
        return []

    import fitz  # PyMuPDF

    page_fn = _PAGE_FN[backend]
    fallback = _resolve_fallback(
        backend
    )  # None unless primary is Tesseract + a stronger backend exists
    dpi = max(72, int(settings.ocr_dpi or 300))
    out: list[str] = []
    escalated = 0
    try:
        doc = fitz.open(str(path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("OCR could not open PDF %s: %s", path, exc)
        return []
    try:
        n = min(doc.page_count, int(settings.ocr_max_pages or 60))
        logger.info(
            "OCR: %s pages of %s via %s @ %ddpi%s",
            n,
            Path(path).name,
            backend,
            dpi,
            f" (escalate->{fallback})" if fallback else "",
        )
        for i, page in enumerate(doc):
            if i >= n:
                break
            try:
                png = _render_gray_png(page, dpi)
                text = page_fn(png) or ""
                if fallback and _page_failed(text):
                    try:
                        alt = _PAGE_FN[fallback](png) or ""
                        if text_quality(alt)[0] > text_quality(text)[0]:
                            logger.info(
                                "OCR page %d: %s output looked garbled -> escalated to %s",
                                i + 1,
                                backend,
                                fallback,
                            )
                            text = alt
                            escalated += 1
                    except Exception as exc:  # noqa: BLE001 — fallback is best-effort
                        logger.warning(
                            "OCR fallback (%s) failed on page %d: %s",
                            fallback,
                            i + 1,
                            exc,
                        )
                out.append(text)
            except Exception as exc:  # noqa: BLE001 — one bad page must not kill the run
                logger.warning("OCR failed on page %d of %s: %s", i + 1, path, exc)
                out.append("")
    finally:
        doc.close()
    if escalated:
        logger.info(
            "OCR: escalated %d/%d page(s) of %s to %s",
            escalated,
            n,
            Path(path).name,
            fallback,
        )
    return out


def ocr_pdf_text(path: str | Path) -> str:
    """Convenience: OCR a PDF and return the concatenated text."""
    return "\n".join(ocr_pdf_pages(path)).strip()
