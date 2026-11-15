"""PDF page classification and PaddleOCR + VietOCR fallback pipeline.

Modeled on ABBYY FineReader 16 Stage 1 (`PdfEngine.dll`, `pdftools.heuristics.dll`,
`PdfTools.Rasterizator.Pdfium.dll`) and Stage 2 (Text Detection + Recognition),
using PaddleOCR (`det=True, rec=False`) for 4-point polygon detection and
VietOCR (`vgg_transformer` / `vgg_seq2seq`) for Vietnamese recognition.
"""

from __future__ import annotations

import inspect
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
import pymupdf as fitz
from PIL import Image

from .layout_engine import (
    ADRTLayoutEngine,
    ADRTPageDocument,
    ParallelColumnsBlock,
    SynthesizedParagraph,
)
from .ocr_engine import PaddleVietOcrEngine, deskew_image_bgr, remove_red_pixels_for_ocr
from .vietnamese import detect_bad_encoding_or_garbled


class PageKind(str, Enum):
    """Classification of a PDF page's extractability."""

    DIGITAL_NATIVE = "digital_native"
    SCANNED_OR_BROKEN_FONT = "scanned_or_broken_font"
    EMPTY = "empty"


@dataclass(frozen=True)
class PageAnalysis:
    """Inspection summary for a single PDF page."""

    page_index: int
    kind: PageKind
    reason: str
    text_char_count: int
    image_count: int
    width_pt: float
    height_pt: float


@dataclass
class OcrTextBlock:
    """Synthesized paragraph or heading block recovered from OCR."""

    bbox: tuple[float, float, float, float]
    text: str
    font_size_pt: float
    is_heading: bool = False
    is_bold: bool = False


@dataclass
class OcrTable:
    """Grid table recovered from a scanned page."""

    bbox: tuple[float, float, float, float]
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class ScannedPageLayout:
    """Structured layout extracted from a scanned or broken-encoding page."""

    page_index: int
    width_pt: float
    height_pt: float
    blocks: list[OcrTextBlock] = field(default_factory=list)
    tables: list[OcrTable] = field(default_factory=list)
    adrt_document: ADRTPageDocument | None = None


def analyze_pdf(pdf_path: str | Path) -> list[PageAnalysis]:
    """Classify every page in a PDF as digital-native, scanned/broken-font, or empty."""
    doc = fitz.open(str(pdf_path))
    analyses: list[PageAnalysis] = []
    try:
        for idx, page in enumerate(doc):
            raw_text = page.get_text("text") or ""
            stripped = raw_text.strip()
            images = page.get_images(full=True)
            drawings = page.get_drawings()

            if len(stripped) < 10:
                if len(images) > 0 or len(drawings) > 5:
                    kind = PageKind.SCANNED_OR_BROKEN_FONT
                    reason = "no_text_layer"
                else:
                    kind = PageKind.EMPTY
                    reason = "blank_page"
            elif detect_bad_encoding_or_garbled(raw_text):
                kind = PageKind.SCANNED_OR_BROKEN_FONT
                reason = "bad_encoding_detected"
            else:
                kind = PageKind.DIGITAL_NATIVE
                reason = "valid_text_layer"

            rect = page.rect
            analyses.append(
                PageAnalysis(
                    page_index=idx,
                    kind=kind,
                    reason=reason,
                    text_char_count=len(stripped),
                    image_count=len(images),
                    width_pt=float(rect.width),
                    height_pt=float(rect.height),
                )
            )
    finally:
        doc.close()
    return analyses


_OCR_ENGINES: dict[str, PaddleVietOcrEngine] = {}


def notify_progress(
    progress_callback,
    step_name: str,
    page_idx: int,
    total_pages: int,
    percent: int,
    message: str,
) -> None:
    """Invoke an optional progress callback supporting both 5-arg and 4-arg signatures."""
    if progress_callback is None:
        return
    clamped = max(0, min(100, int(percent)))
    try:
        sig = inspect.signature(progress_callback)
        params = list(sig.parameters.values())
        has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
        if has_varargs or len(params) >= 5:
            progress_callback(step_name, page_idx, total_pages, clamped, message)
        else:
            progress_callback(page_idx, total_pages, message, clamped)
    except (ValueError, TypeError):
        progress_callback(step_name, page_idx, total_pages, clamped, message)


def get_ocr_engine(model_name: str | None = None) -> PaddleVietOcrEngine:
    """Return a cached `PaddleVietOcrEngine` singleton keyed by resolved VietOCR model name."""
    resolved = model_name or os.environ.get("PDF2WORD_VIETOCR_MODEL", "vgg_transformer")
    if resolved not in _OCR_ENGINES:
        _OCR_ENGINES[resolved] = PaddleVietOcrEngine(model_name=resolved)
    _OCR_ENGINES["default"] = _OCR_ENGINES[resolved]
    return _OCR_ENGINES[resolved]


def preload_ocr_engine(
    model_name: str | None = None,
    progress_callback: Callable[[int, str], None] | None = None,
) -> PaddleVietOcrEngine:
    """Preload and warm up PaddleOCR + VietOCR + ADRTLayoutEngine into memory."""

    def _report(pct: int, msg: str) -> None:
        if progress_callback is not None:
            progress_callback(max(0, min(100, int(pct))), msg)

    _report(10, "Đang kiểm tra môi trường & thiết bị phần cứng (CUDA/CPU)...")
    # Import PyTorch before PaddlePaddle on Windows to avoid OpenMP/MKL DLL conflicts.
    import torch  # noqa: F401

    engine = get_ocr_engine(model_name=model_name)

    _report(35, "Đang nạp mô hình phát hiện vùng chữ PaddleOCR...")
    engine.detector._get_detector()

    _report(65, "Đang nạp mô hình nhận diện Tiếng Việt VietOCR...")
    engine.recognizer._get_predictor()

    _report(85, "Đang khởi động bộ tái tạo cấu trúc Word ADRT & chạy Warm-up Inference...")
    dummy_bgr = np.full((120, 420, 3), 255, dtype=np.uint8)
    cv2.putText(dummy_bgr, "VIET NAM 2026", (24, 72), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 0), 2)
    engine.detector.detect(dummy_bgr)
    engine.recognizer.recognize(Image.fromarray(dummy_bgr))
    _ = ADRTLayoutEngine(dpi=300)

    _report(100, "Sẵn sàng!")
    return engine


def _cell_has_ink(image_bgr: np.ndarray, bbox: tuple[int, int, int, int]) -> bool:
    """Return True if an interior cell crop contains dark foreground text pixels."""
    cx0, cy0, cx1, cy1 = bbox
    pad = 4
    crop = image_bgr[max(0, cy0 + pad) : max(0, cy1 - pad), max(0, cx0 + pad) : max(0, cx1 - pad)]
    if crop.size == 0:
        return False
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return int(np.sum(gray < 200)) >= 12


def ocr_scanned_page(
    pdf_path: str | Path,
    page_index: int,
    lang: str = "vie",
    dpi: int = 300,
    ocr_model: str | None = None,
    progress_callback=None,
    total_pages: int = 1,
    fitz_doc: fitz.Document | None = None,
) -> ScannedPageLayout:
    """Rasterize a scanned/broken-font PDF page and run PaddleOCR + VietOCR + 6-layer ADRT synthesis.

    Uses PaddleOCR (`det=True, rec=False`) + 4-point perspective crop + VietOCR
    recognition, avoiding repeated detection inside table cells by matching
    page-level detections into cell polygons (with targeted VietOCR fallback for
    small ink-containing cells missed by global detection), then reconstructs full
    page geometry, merged table grids, parallel layout blocks, paragraphs, typography,
    and headers/footers via `ADRTLayoutEngine`.
    """
    _ = lang  # Kept in signature for API compatibility; VietOCR specializes in Vietnamese
    scale = 72.0 / float(dpi)
    p_num = page_index + 1
    total_p = max(1, int(total_pages))
    base_pct = 5 + int(85.0 * page_index / total_p)
    span_pct = 85.0 / total_p

    notify_progress(
        progress_callback,
        "preprocess",
        page_index,
        total_p,
        int(base_pct + 0.10 * span_pct),
        f"Trang {p_num}/{total_p}: Phân tích hình học, khử nghiêng & tách con dấu đỏ...",
    )

    engine = get_ocr_engine(model_name=ocr_model)
    adrt_engine = ADRTLayoutEngine(dpi=dpi)

    owns_doc = fitz_doc is None
    doc = fitz.open(str(pdf_path)) if owns_doc else fitz_doc
    try:
        page = doc[page_index]
        width_pt = float(page.rect.width)
        height_pt = float(page.rect.height)
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        img_rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        image_bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)

        # Deskew slight scan tilt so OCR detection, table grid, and seal extraction align cleanly
        image_bgr, _ = deskew_image_bgr(image_bgr)
        ocr_clean_bgr = remove_red_pixels_for_ocr(image_bgr)

        notify_progress(
            progress_callback,
            "detect",
            page_index,
            total_p,
            int(base_pct + 0.30 * span_pct),
            f"Trang {p_num}/{total_p}: Đang phát hiện khung chữ bằng PaddleOCR...",
        )

        recognized_notified = False

        def _on_ocr_stage(stage: str, count: int) -> None:
            nonlocal recognized_notified
            if stage == "recognize":
                recognized_notified = True
                notify_progress(
                    progress_callback,
                    "recognize",
                    page_index,
                    total_p,
                    int(base_pct + 0.55 * span_pct),
                    f"Trang {p_num}/{total_p}: Đang nhận diện Tiếng Việt bằng VietOCR ({count} vùng chữ)...",
                )

        # Single full-page PaddleOCR detection + VietOCR batch recognition on red-stripped image
        rec_params = inspect.signature(engine.recognize_image).parameters
        if "on_stage" in rec_params:
            detections = engine.recognize_image(ocr_clean_bgr, strip_red_seals=False, on_stage=_on_ocr_stage)
        else:
            detections = engine.recognize_image(ocr_clean_bgr, strip_red_seals=False)

        if not recognized_notified:
            notify_progress(
                progress_callback,
                "recognize",
                page_index,
                total_p,
                int(base_pct + 0.55 * span_pct),
                f"Trang {p_num}/{total_p}: Đang nhận diện Tiếng Việt bằng VietOCR ({len(detections)} vùng chữ)...",
            )

        # Pre-detect bordered table cells to run targeted VietOCR fallback on inked empty cells
        raw_tables = adrt_engine.detect_bordered_tables(image_bgr, fitz_page=page)
        extra_table_cell_texts: dict[tuple[int, int, int], str] = {}
        fallback_crops: list[tuple[int, int, int, Image.Image]] = []

        det_centers_pt = [
            (0.5 * (box[0] + box[2]) * scale, 0.5 * (box[1] + box[3]) * scale)
            for box, _ in detections
        ]
        for t_idx, tbl in enumerate(raw_tables):
            for cell in tbl["cells"]:
                bx0, by0, bx1, by1 = cell["bbox"]
                has_det = any(
                    (bx0 - 3.0) <= cx <= (bx1 + 3.0) and (by0 - 3.0) <= cy <= (by1 + 3.0)
                    for cx, cy in det_centers_pt
                )
                if not has_det:
                    px_box = (
                        int(round(bx0 / scale)),
                        int(round(by0 / scale)),
                        int(round(bx1 / scale)),
                        int(round(by1 / scale)),
                    )
                    if _cell_has_ink(ocr_clean_bgr, px_box):
                        pad = 3
                        crop_bgr = ocr_clean_bgr[
                            max(0, px_box[1] + pad) : max(0, px_box[3] - pad),
                            max(0, px_box[0] + pad) : max(0, px_box[2] - pad),
                        ]
                        if crop_bgr.size > 0:
                            crop_pil = Image.fromarray(cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB))
                            fallback_crops.append((t_idx, cell["row"], cell["col"], crop_pil))

        if fallback_crops:
            fallback_texts = engine.recognizer.recognize_batch([item[3] for item in fallback_crops])
            for (t_idx, r_idx, c_idx, _), text in zip(fallback_crops, fallback_texts):
                if text:
                    extra_table_cell_texts[(t_idx, r_idx, c_idx)] = text

        notify_progress(
            progress_callback,
            "layout",
            page_index,
            total_p,
            int(base_pct + 0.80 * span_pct),
            f"Trang {p_num}/{total_p}: Đang tái tạo cấu trúc đoạn, bảng & ô Text trôi nổi...",
        )

        adrt_doc = adrt_engine.analyze_page(
            page_index=page_index,
            width_pt=width_pt,
            height_pt=height_pt,
            image_bgr=image_bgr,
            detections=detections,
            fitz_page=page,
            extra_table_cell_texts=extra_table_cell_texts,
            precomputed_tables=raw_tables,
        )

        notify_progress(
            progress_callback,
            "layout",
            page_index,
            total_p,
            int(base_pct + 0.98 * span_pct),
            f"[✓] Trang {p_num}/{total_p} hoàn tất ({len(detections)} hộp chữ, {len(adrt_doc.tables)} bảng)",
        )
    finally:
        if owns_doc:
            doc.close()

    # Populate backward-compatible `blocks` and `tables` summaries alongside `adrt_document`
    blocks: list[OcrTextBlock] = []
    for el in adrt_doc.body_elements:
        if isinstance(el, SynthesizedParagraph):
            blocks.append(
                OcrTextBlock(
                    bbox=el.bbox,
                    text=el.text,
                    font_size_pt=el.font_size_pt,
                    is_heading=el.is_heading,
                    is_bold=el.is_bold,
                )
            )
        elif isinstance(el, ParallelColumnsBlock):
            for col_p in (*el.left_paragraphs, *el.right_paragraphs):
                blocks.append(
                    OcrTextBlock(
                        bbox=col_p.bbox,
                        text=col_p.text,
                        font_size_pt=col_p.font_size_pt,
                        is_heading=col_p.is_heading,
                        is_bold=col_p.is_bold,
                    )
                )

    ocr_tables: list[OcrTable] = []
    for tbl in adrt_doc.tables:
        grid = [["" for _ in range(tbl.cols_count)] for _ in range(tbl.rows_count)]
        for cell in tbl.cells:
            grid[cell.row][cell.col] = cell.text
        ocr_tables.append(OcrTable(bbox=tbl.bbox, rows=grid))

    return ScannedPageLayout(
        page_index=page_index,
        width_pt=width_pt,
        height_pt=height_pt,
        blocks=blocks,
        tables=ocr_tables,
        adrt_document=adrt_doc,
    )
