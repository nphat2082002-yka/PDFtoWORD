"""Public package API for Vietnamese PDF-to-DOCX conversion."""

from __future__ import annotations

from pathlib import Path

__version__ = "1.0.0"
__author__ = "Huỳnh Ngọc Phát"
__copyright__ = "© 2026 Bản quyền bởi Huỳnh Ngọc Phát"

from .abbyy_adapter import AbbyyLocalAdapter
from .analyzer import PageAnalysis, PageKind, analyze_pdf, get_ocr_engine, preload_ocr_engine
from .converter import CleanRoomConverter, ConversionResult, LayoutMode
from .docx_synthesizer import DocxStructureSynthesizer
from .layout_engine import (
    ADRTLayoutEngine,
    ADRTPageDocument,
    FigureBlock,
    FloatingTextBox,
    FloatingTextBoxDetector,
    LogicalLine,
    PageGeometry,
    ParallelColumnsBlock,
    ReconstructedTable,
    SynthesizedParagraph,
    TableCellBox,
    WordSpan,
    should_merge_into_paragraph,
)
from .ocr_engine import (
    DetectedTextRegion,
    PaddleTextDetector,
    PaddleVietOcrEngine,
    VietOCRRecognizer,
    deskew_image_bgr,
    merge_same_row_polygons,
    order_quad_points,
    perspective_crop_polygon,
    remove_red_pixels_for_ocr,
    sort_polygons_reading_order,
)
from .vietnamese import (
    VietnameseAdminPostProcessor,
    detect_bad_encoding_or_garbled,
    is_browser_or_viewer_noise,
    is_vietnamese_nfc,
    normalize_vietnamese_nfc,
    postprocess_vietnamese_admin_line,
)


def convert_pdf_to_docx(
    input_pdf: str | Path,
    output_docx: str | Path,
    mode: LayoutMode | str = LayoutMode.EDITABLE,
    engine: str = "cleanroom",
    lang: str = "vie",
    ocr_dpi: int = 300,
    ocr_model: str | None = None,
    progress_callback=None,
    cancel_event=None,
) -> ConversionResult:
    """Convert a Vietnamese PDF file to a structured Unicode NFC `.docx` in one call.

    Parameters
    ----------
    input_pdf:
        Path to the source PDF file.
    output_docx:
        Destination `.docx` file path.
    mode:
        Layout retention mode: `'editable'` (default), `'formatted'`, or `'exact'`.
    engine:
        `'cleanroom'` (default standalone 5-stage pipeline), `'abbyy'` (local ABBYY CLI),
        or `'auto'` (tries cleanroom).
    lang:
        Primary language (`'vie'` default).
    ocr_dpi:
        Rasterization DPI for scanned or broken-encoding pages (default 300).
    ocr_model:
        VietOCR backbone model (`'vgg_transformer'` default, or `'vgg_seq2seq'` for faster CPU).
    progress_callback:
        Optional callback invoked during conversion with `(step_name, page_idx, total_pages, percent, message)`.
    """
    selected = engine.lower().strip()
    if selected == "abbyy":
        return AbbyyLocalAdapter().convert(
            input_pdf=input_pdf,
            output_docx=output_docx,
            mode=mode,
            lang="Vietnamese" if lang.lower() in ("vie", "vi", "vietnamese") else lang,
            fallback_to_cleanroom=True,
        )
    if selected in ("cleanroom", "auto"):
        return CleanRoomConverter().convert(
            input_pdf=input_pdf,
            output_docx=output_docx,
            mode=mode,
            lang=lang,
            ocr_dpi=ocr_dpi,
            ocr_model=ocr_model,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )
    raise ValueError(f"Unsupported engine '{engine}'. Choose 'cleanroom', 'abbyy', or 'auto'.")


__all__ = [
    "__author__",
    "__copyright__",
    "__version__",
    "ADRTLayoutEngine",
    "ADRTPageDocument",
    "AbbyyLocalAdapter",
    "CleanRoomConverter",
    "ConversionResult",
    "DetectedTextRegion",
    "DocxStructureSynthesizer",
    "FigureBlock",
    "FloatingTextBox",
    "FloatingTextBoxDetector",
    "LayoutMode",
    "LogicalLine",
    "PaddleTextDetector",
    "PaddleVietOcrEngine",
    "PageAnalysis",
    "PageGeometry",
    "PageKind",
    "ParallelColumnsBlock",
    "ReconstructedTable",
    "SynthesizedParagraph",
    "TableCellBox",
    "VietOCRRecognizer",
    "VietnameseAdminPostProcessor",
    "WordSpan",
    "analyze_pdf",
    "convert_pdf_to_docx",
    "deskew_image_bgr",
    "detect_bad_encoding_or_garbled",
    "get_ocr_engine",
    "is_browser_or_viewer_noise",
    "is_vietnamese_nfc",
    "merge_same_row_polygons",
    "normalize_vietnamese_nfc",
    "order_quad_points",
    "perspective_crop_polygon",
    "postprocess_vietnamese_admin_line",
    "preload_ocr_engine",
    "remove_red_pixels_for_ocr",
    "should_merge_into_paragraph",
    "sort_polygons_reading_order",
]
