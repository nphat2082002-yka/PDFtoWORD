"""Clean-room 5-stage PDF-to-DOCX layout & OpenXML converter.

Modeled on ABBYY FineReader 16 Stage 3 (`TableCapture.dll`, `Synthesis.dll`)
and Stage 5 (`Export.Common.dll`, `export.wp.dll`).
"""

from __future__ import annotations

import os
import re
import statistics
import tempfile
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from pdf2docx import Converter as Pdf2DocxConverter
import pymupdf as fitz

from .analyzer import (
    PageAnalysis,
    PageKind,
    ScannedPageLayout,
    analyze_pdf,
    notify_progress,
    ocr_scanned_page,
)
from .docx_synthesizer import DocxStructureSynthesizer
from .vietnamese import is_vietnamese_nfc, normalize_vietnamese_nfc

_PDF_FONT_SUBSET_RE = re.compile(r"^[A-Z]{6}\+")
_DEFAULT_VIET_FONT = "Times New Roman"


class LayoutMode(str, Enum):
    """DOCX layout retention modes corresponding to ABBYY export.wp.dll."""

    EDITABLE = "editable"
    FORMATTED = "formatted"
    EXACT = "exact"


@dataclass(frozen=True)
class ConversionResult:
    """Summary metrics for a PDF-to-DOCX conversion run."""

    input_path: str
    output_path: str
    engine: str
    mode: str
    pages_total: int
    digital_pages: int
    ocr_pages: int
    empty_pages: int
    tables_count: int
    paragraphs_count: int
    nfc_verified: bool


def _clean_font_name(name: str | None) -> str:
    """Strip 6-letter PDF subset tags (e.g. `ABCDEE+TimesNewRomanPSMT`) and map cleanly."""
    if not name:
        return _DEFAULT_VIET_FONT
    cleaned = _PDF_FONT_SUBSET_RE.sub("", name).strip()
    lower = cleaned.lower()
    if "times" in lower or "serif" in lower:
        return "Times New Roman"
    if "arial" in lower or "helvetica" in lower:
        return "Arial"
    if "calibri" in lower:
        return "Calibri"
    if "cambria" in lower:
        return "Cambria"
    if "tahoma" in lower:
        return "Tahoma"
    if "courier" in lower or "consolas" in lower:
        return "Courier New"
    return cleaned or _DEFAULT_VIET_FONT


def _iter_all_paragraphs(doc: Document) -> Iterable:
    """Yield every paragraph across document body, tables (recursively), headers, and footers."""
    yield from doc.paragraphs

    def _walk_tables(tables):
        for table in tables:
            for row in table.rows:
                for cell in row.cells:
                    yield from cell.paragraphs
                    if cell.tables:
                        yield from _walk_tables(cell.tables)

    yield from _walk_tables(doc.tables)

    for section in doc.sections:
        for hdr_ftr in (section.header, section.footer, section.first_page_header, section.first_page_footer):
            if hdr_ftr is not None:
                yield from hdr_ftr.paragraphs
                if hdr_ftr.tables:
                    yield from _walk_tables(hdr_ftr.tables)


def _run_has_non_text_xml(run) -> bool:
    """Return True if a run wraps an embedded image, drawing, or field XML."""
    el = run._element
    for tag in ("drawing", "pict", "object", "fldChar", "instrText"):
        if el.find(qn(f"w:{tag}")) is not None:
            return True
    return False


def _run_style_signature(run) -> tuple:
    color_rgb = run.font.color.rgb if run.font.color is not None else None
    size_pt = run.font.size.pt if run.font.size is not None else None
    return (
        bool(run.bold),
        bool(run.italic),
        bool(run.underline),
        _clean_font_name(run.font.name),
        size_pt,
        color_rgb,
    )


def _normalize_paragraph_runs(paragraph, merge_runs: bool) -> None:
    """Normalize Vietnamese NFC across all runs, merging fragmented same-style runs."""
    runs = list(paragraph.runs)
    if not runs:
        return

    if merge_runs:
        prev_run = None
        prev_sig = None
        for run in runs:
            if _run_has_non_text_xml(run):
                prev_run = None
                prev_sig = None
                continue
            sig = _run_style_signature(run)
            if prev_run is not None and sig == prev_sig:
                prev_run.text = normalize_vietnamese_nfc(prev_run.text + run.text)
                run.text = ""
                run._element.getparent().remove(run._element)
            else:
                run.text = normalize_vietnamese_nfc(run.text)
                if run.font.name:
                    run.font.name = _clean_font_name(run.font.name)
                prev_run = run
                prev_sig = sig
    else:
        for run in runs:
            if not _run_has_non_text_xml(run):
                run.text = normalize_vietnamese_nfc(run.text)
                if run.font.name:
                    run.font.name = _clean_font_name(run.font.name)


def _apply_heading_clusters(doc: Document) -> None:
    """Detect heading clusters by font-size & boldness (modeled on Synthesis.dll `CHeadingClusterizer`)."""
    body_sizes: list[float] = []
    for p in doc.paragraphs:
        if not p.text.strip():
            continue
        sizes = [r.font.size.pt for r in p.runs if r.font.size is not None and r.text.strip()]
        if sizes:
            body_sizes.append(statistics.median(sizes))

    if not body_sizes:
        return

    median_body_pt = statistics.median(body_sizes)
    if median_body_pt <= 0:
        return

    for p in doc.paragraphs:
        text = p.text.strip()
        if not text or len(text) > 140:
            continue
        if p.style and p.style.name.startswith("Heading"):
            continue
        non_empty_runs = [r for r in p.runs if r.text.strip() and not _run_has_non_text_xml(r)]
        if not non_empty_runs:
            continue
        sizes = [r.font.size.pt for r in non_empty_runs if r.font.size is not None]
        if not sizes:
            continue
        p_size = max(sizes)
        all_bold = all(bool(r.bold) for r in non_empty_runs)

        if p_size >= median_body_pt * 1.45:
            p.style = doc.styles["Heading 1"]
        elif p_size >= median_body_pt * 1.22 or (all_bold and p_size >= median_body_pt * 1.08 and not text.endswith(".")):
            p.style = doc.styles["Heading 2"]


def _set_table_borders(table) -> None:
    """Ensure crisp OpenXML `w:tblBorders` so Word tables render reliably."""
    tbl_pr = table._tbl.tblPr
    existing = tbl_pr.find(qn("w:tblBorders"))
    if existing is not None:
        return
    borders = OxmlElement("w:tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), "888888")
        borders.append(el)
    tbl_pr.append(borders)


def _append_scanned_layout_to_doc(doc: Document, layout: ScannedPageLayout, add_page_break: bool) -> None:
    """Synthesize real OpenXML paragraphs, parallel blocks, tables & headers/footers from OCR layout."""
    if layout.adrt_document is not None:
        DocxStructureSynthesizer().append_page(
            doc,
            layout.adrt_document,
            is_first_page=(not add_page_break),
        )
        return

    # Minimal fallback for synthetic ScannedPageLayout objects constructed in tests without adrt_document
    if add_page_break:
        doc.add_page_break()
    for blk in sorted(layout.blocks, key=lambda b: b.bbox[1]):
        text = normalize_vietnamese_nfc(blk.text, collapse_spaces=True)
        if not text:
            continue
        p = doc.add_paragraph()
        run = p.add_run(text)
        run.font.name = _DEFAULT_VIET_FONT
        run.font.size = Pt(blk.font_size_pt)
        run.bold = blk.is_bold


def _append_docx_body(target_doc: Document, source_docx_path: str, add_page_break: bool) -> None:
    """Append body elements (paragraphs & tables) from a temporary partial DOCX into target_doc."""
    src = Document(source_docx_path)
    if add_page_break:
        target_doc.add_page_break()
    src_body = src.element.body
    tgt_body = target_doc.element.body
    sect_pr = tgt_body.find(qn("w:sectPr"))
    for child in list(src_body):
        if child.tag == qn("w:sectPr"):
            continue
        if sect_pr is not None:
            sect_pr.addprevious(child)
        else:
            tgt_body.append(child)


def _postprocess_docx(
    docx_path: Path,
    mode: LayoutMode,
    apply_heading_clusters: bool = True,
) -> tuple[int, int, bool]:
    """Run Stage 4 & 5 Vietnamese NFC normalization and ADRT structural refinement on DOCX."""
    doc = Document(str(docx_path))

    merge_runs = mode in (LayoutMode.EDITABLE, LayoutMode.FORMATTED)
    for paragraph in _iter_all_paragraphs(doc):
        _normalize_paragraph_runs(paragraph, merge_runs=merge_runs)

    for table in doc.tables:
        _set_table_borders(table)

    if mode == LayoutMode.EDITABLE and apply_heading_clusters:
        _apply_heading_clusters(doc)

    # Verify 100% NFC across all paragraphs in document
    all_texts = [p.text for p in _iter_all_paragraphs(doc) if p.text]
    nfc_ok = all(is_vietnamese_nfc(t) for t in all_texts)

    doc.save(str(docx_path))
    return len(doc.tables), len(doc.paragraphs), nfc_ok


class CleanRoomConverter:
    """5-stage clean-room Vietnamese PDF-to-DOCX converter."""

    def convert(
        self,
        input_pdf: str | Path,
        output_docx: str | Path,
        mode: LayoutMode | str = LayoutMode.EDITABLE,
        lang: str = "vie",
        ocr_dpi: int = 300,
        ocr_model: str | None = None,
        progress_callback=None,
        cancel_event: threading.Event | None = None,
    ) -> ConversionResult:
        """Convert `input_pdf` to `output_docx` using hybrid digital/OCR + Vietnamese NFC pipeline."""
        in_path = Path(input_pdf).resolve()
        out_path = Path(output_docx).resolve()
        if not in_path.is_file():
            raise FileNotFoundError(f"Input PDF not found: {in_path}")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        notify_progress(progress_callback, "preprocess", 0, 1, 0, "Đang phân tích cấu trúc trang PDF...")

        layout_mode = LayoutMode(mode) if isinstance(mode, str) else mode
        analyses: list[PageAnalysis] = analyze_pdf(in_path)
        total_pages = max(1, len(analyses))

        digital_indices = [a.page_index for a in analyses if a.kind == PageKind.DIGITAL_NATIVE]
        ocr_indices = [a.page_index for a in analyses if a.kind == PageKind.SCANNED_OR_BROKEN_FONT]
        empty_indices = [a.page_index for a in analyses if a.kind == PageKind.EMPTY]

        notify_progress(
            progress_callback,
            "preprocess",
            0,
            total_pages,
            5,
            f"Đã phân tích {len(analyses)} trang PDF (OCR: {len(ocr_indices)}, Digital: {len(digital_indices)}).",
        )

        pdf2docx_kwargs = {
            "parse_lattice_table": True,
            "parse_stream_table": layout_mode != LayoutMode.EXACT,
            "delete_end_line_hyphen": True,
        }

        if not ocr_indices:
            # Pure digital PDF (or digital + blank pages): convert directly with pdf2docx
            notify_progress(
                progress_callback,
                "layout",
                0,
                total_pages,
                35,
                f"Đang chuyển đổi {len(analyses)} trang PDF văn bản gốc sang cấu trúc Word...",
            )
            cv = Pdf2DocxConverter(str(in_path))
            try:
                cv.convert(str(out_path), **pdf2docx_kwargs)
            finally:
                cv.close()
            notify_progress(
                progress_callback,
                "layout",
                total_pages - 1,
                total_pages,
                88,
                f"[✓] Đã dựng xong bố cục {len(analyses)} trang văn bản gốc.",
            )
        elif not digital_indices:
            # Pure scanned / broken-font PDF: reuse one open PyMuPDF handle across pages
            doc = Document()
            fitz_doc = fitz.open(str(in_path))
            try:
                for pos, page_idx in enumerate(ocr_indices):
                    if cancel_event is not None and cancel_event.is_set():
                        raise InterruptedError("Quá trình chuyển đổi đã được hủy bởi người dùng.")
                    layout = ocr_scanned_page(
                        in_path,
                        page_idx,
                        lang=lang,
                        dpi=ocr_dpi,
                        ocr_model=ocr_model,
                        progress_callback=progress_callback,
                        total_pages=total_pages,
                        fitz_doc=fitz_doc,
                    )
                    _append_scanned_layout_to_doc(doc, layout, add_page_break=(pos > 0))
            finally:
                fitz_doc.close()
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Quá trình chuyển đổi đã được hủy bởi người dùng.")
            doc.save(str(out_path))
        else:
            # Mixed PDF: process contiguous page segments in exact page order
            doc = Document()
            fitz_doc = fitz.open(str(in_path))
            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    has_content = False
                    i = 0
                    while i < len(analyses):
                        if cancel_event is not None and cancel_event.is_set():
                            raise InterruptedError("Quá trình chuyển đổi đã được hủy bởi người dùng.")
                        item = analyses[i]
                        if item.kind == PageKind.EMPTY:
                            i += 1
                            continue
                        if item.kind == PageKind.DIGITAL_NATIVE:
                            j = i
                            seg_pages: list[int] = []
                            while j < len(analyses) and analyses[j].kind == PageKind.DIGITAL_NATIVE:
                                seg_pages.append(analyses[j].page_index)
                                j += 1
                            seg_docx = os.path.join(tmpdir, f"seg_{i}.docx")
                            seg_pct = 5 + int(85.0 * seg_pages[0] / total_pages)
                            notify_progress(
                                progress_callback,
                                "layout",
                                seg_pages[0],
                                total_pages,
                                seg_pct,
                                f"Trang {seg_pages[0] + 1}-{seg_pages[-1] + 1}/{total_pages}: Đang tái tạo phân đoạn văn bản gốc...",
                            )
                            cv = Pdf2DocxConverter(str(in_path))
                            try:
                                cv.convert(seg_docx, pages=seg_pages, **pdf2docx_kwargs)
                            finally:
                                cv.close()
                            _append_docx_body(doc, seg_docx, add_page_break=has_content)
                            has_content = True
                            i = j
                        else:
                            layout = ocr_scanned_page(
                                in_path,
                                item.page_index,
                                lang=lang,
                                dpi=ocr_dpi,
                                ocr_model=ocr_model,
                                progress_callback=progress_callback,
                                total_pages=total_pages,
                                fitz_doc=fitz_doc,
                            )
                            _append_scanned_layout_to_doc(doc, layout, add_page_break=has_content)
                            has_content = True
                            i += 1
            finally:
                fitz_doc.close()
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Quá trình chuyển đổi đã được hủy bởi người dùng.")
            doc.save(str(out_path))

        notify_progress(
            progress_callback,
            "export",
            total_pages - 1,
            total_pages,
            92,
            "Đang chuẩn hóa thể thức Tiếng Việt (NFC) & đóng gói OpenXML (.docx)...",
        )

        tables_count, paragraphs_count, nfc_ok = _postprocess_docx(
            out_path,
            layout_mode,
            apply_heading_clusters=bool(digital_indices),
        )

        notify_progress(
            progress_callback,
            "export",
            total_pages,
            total_pages,
            100,
            f"Hoàn tất chuyển đổi {len(analyses)} trang sang Word (.docx)!",
        )

        return ConversionResult(
            input_path=str(in_path),
            output_path=str(out_path),
            engine="cleanroom",
            mode=layout_mode.value,
            pages_total=len(analyses),
            digital_pages=len(digital_indices),
            ocr_pages=len(ocr_indices),
            empty_pages=len(empty_indices),
            tables_count=tables_count,
            paragraphs_count=paragraphs_count,
            nfc_verified=nfc_ok,
        )
