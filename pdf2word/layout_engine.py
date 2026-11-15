"""ADRT-style 5-layer geometric document layout & typography analysis engine.

Modeled on ABBYY FineReader 16 `Synthesis.dll`, `TableCapture.dll`, and
`pdftools.heuristics.dll`:
1. Page Geometry & Margin Calibration
2. Hybrid Vector + OpenCV Table Grid (with merged cell gridSpan/vMerge) & Figures
3. Horizontal LogicalLine Grouping, Header/Footer Separation & 2-Column Parallel Blocks
4. Anti-Hard-Return Paragraph Reconstruction (`should_merge_into_paragraph`)
5. Geometric Alignment, Indentation, Spacing, Font Size & Bold Inference
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Sequence, Union

import cv2
import numpy as np
import pymupdf as fitz

from .vietnamese import (
    VietnameseAdminPostProcessor,
    is_browser_or_viewer_noise,
    normalize_vietnamese_nfc,
    repair_line_wraps,
)

STANDARD_WORD_FONT_SIZES: tuple[float, ...] = (
    8.0,
    9.0,
    10.0,
    10.5,
    11.0,
    12.0,
    13.0,
    14.0,
    16.0,
    18.0,
    20.0,
    22.0,
    24.0,
    28.0,
)

# Vietnamese list items, clauses, and structural markers that start a new paragraph
_VIET_LIST_OR_CLAUSE_RE = re.compile(
    r"^\s*(?:"
    r"\d{1,2}\.\d{1,2}[\.\)]?"
    r"|\d{1,3}[\.\)]"
    r"|[a-zđ][\.\)]"
    r"|[IVXLCDM]{1,6}\."
    r"|[-+•*▪–—]"
    r"|(?:Điều|Khoản|Mục)\s+\d+[\.\:\)]?"
    r"|(?:Chương|Phần)\s+[IVXLCDM\d]+[\.\:\)]?"
    r"|Nơi\s+nhận\s*:"
    r"|Số\s*:"
    r")(?:\s|$)"
)

_PAGE_NUMBER_OR_FOOTER_RE = re.compile(
    r"^\s*(?:"
    r"(?:Trang|Page|Tr\.?)\s*\d+(?:\s*/\s*\d+)?"
    r"|-\s*\d+\s*-"
    r"|\d{1,4}\s*/\s*\d{1,4}"
    r"|\d{1,4}"
    r")\s*$",
    re.IGNORECASE,
)

_SIGNATURE_ROLE_RE = re.compile(
    r"^\s*(?:KT\.|TM\.|TL\.|TUQ\.)?\s*(?:PHÓ\s+)?"
    r"(?:GIÁM\s+THỊ|GIÁM\s+ĐỐC|THỦ\s+TRƯỞNG|CHỈ\s+HUY(?:\s+TRƯỞNG)?|CHỦ\s+TỊCH|TRƯỞNG\s+(?:PHÒNG|BAN|ĐOÀN)|HIỆU\s+TRƯỞNG)\b",
    re.IGNORECASE,
)

_DOC_TITLE_RE = re.compile(
    r"^\s*(?:KẾ\s+HOẠCH|QUYẾT\s+ĐỊNH|THÔNG\s+BÁO|BÁO\s+CÁO|TỜ\s+TRÌNH|CHƯƠNG\s+TRÌNH|PHƯƠNG\s+ÁN|QUY\s+CHẾ|HƯỚNG\s+DẪN|BIÊN\s+BẢN)\s*$"
)

_FLOATING_ADMIN_TAG_RE = re.compile(
    r"^\s*(?:MẬT|TỐI\s+MẬT|TUYỆT\s+MẬT|KHẨN|THƯỢNG\s+KHẨN|HỎA\s+TỐC|BẢN\s+SỐ\s*:?.*)\s*$",
    re.IGNORECASE,
)
_CONFIDENTIAL_OR_URGENT_STAMP_RE = re.compile(
    r"^\s*(?:MẬT|TỐI\s+MẬT|TUYỆT\s+MẬT|KHẨN|THƯỢNG\s+KHẨN|HỎA\s+TỐC)\s*$",
    re.IGNORECASE,
)


def _is_signer_name_span(text: str) -> bool:
    """Return True if a right-column span looks like a Vietnamese rank/signer proper name."""
    s = normalize_vietnamese_nfc(text, collapse_spaces=True).strip()
    if not s or len(s) < 5 or len(s) > 60:
        return False
    if s[0] in "(-+*0123456789":
        return False
    if _SIGNATURE_ROLE_RE.match(s):
        return False
    if s.isupper():
        return False
    words = s.split()
    if len(words) < 2 or len(words) > 8:
        return False
    cap_words = sum(1 for w in words if w and w[0].isupper())
    has_lower = any(ch.islower() for ch in s)
    return has_lower and cap_words >= 2


@dataclass
class PageGeometry:
    """Calibrated PDF page dimensions and Word section margins (points)."""

    width_pt: float
    height_pt: float
    is_landscape: bool
    top_margin_pt: float
    bottom_margin_pt: float
    left_margin_pt: float
    right_margin_pt: float
    content_left_pt: float
    content_right_pt: float
    content_top_pt: float
    content_bottom_pt: float

    @property
    def content_width_pt(self) -> float:
        return max(1.0, self.content_right_pt - self.content_left_pt)


@dataclass
class WordSpan:
    """Single OCR detection span in PDF point coordinates with typography cues."""

    bbox: tuple[float, float, float, float]  # (x0, y0, x1, y1)
    text: str
    font_size_pt: float = 11.0
    is_bold: bool = False
    stroke_score: float = 0.0


@dataclass
class LogicalLine:
    """A single horizontal visual line composed of ordered WordSpans."""

    bbox: tuple[float, float, float, float]
    text: str
    words: list[WordSpan] = field(default_factory=list)
    font_size_pt: float = 11.0
    is_bold: bool = False
    baseline_y: float = 0.0


@dataclass
class SynthesizedParagraph:
    """Reconstructed OpenXML-ready paragraph with typography and indentation."""

    bbox: tuple[float, float, float, float]
    text: str
    lines: list[LogicalLine] = field(default_factory=list)
    alignment: str = "LEFT"  # LEFT | CENTER | RIGHT | JUSTIFY
    left_indent_pt: float = 0.0
    right_indent_pt: float = 0.0
    first_line_indent_pt: float = 0.0
    space_before_pt: float = 0.0
    space_after_pt: float = 3.0
    line_spacing: float = 1.15
    font_size_pt: float = 11.0
    is_bold: bool = False
    is_italic: bool = False
    has_bottom_border: bool = False
    bold_prefix_len: int = 0
    is_heading: bool = False
    heading_level: int = 0
    is_list_item: bool = False


@dataclass
class TableCellBox:
    """Single logical table cell supporting vertical and horizontal merging."""

    row: int
    col: int
    row_span: int
    col_span: int
    bbox: tuple[float, float, float, float]
    paragraphs: list[SynthesizedParagraph] = field(default_factory=list)
    text: str = ""


@dataclass
class ReconstructedTable:
    """Structured bordered table with proportional column widths and merged cells."""

    bbox: tuple[float, float, float, float]
    rows_count: int
    cols_count: int
    col_widths_pt: list[float]
    row_heights_pt: list[float]
    cells: list[TableCellBox] = field(default_factory=list)
    has_borders: bool = True


@dataclass
class ParallelColumnsBlock:
    """Borderless 2-column layout table for side-by-side headers, signatures, or dual columns."""

    bbox: tuple[float, float, float, float]
    left_paragraphs: list[SynthesizedParagraph] = field(default_factory=list)
    right_paragraphs: list[SynthesizedParagraph] = field(default_factory=list)
    left_width_pt: float = 240.0
    right_width_pt: float = 240.0
    gap_width_pt: float = 20.0
    right_figure: FigureBlock | None = None
    right_figure_after_idx: int = -1


@dataclass
class FigureBlock:
    """Embedded non-text image / figure extracted at its vertical position."""

    bbox: tuple[float, float, float, float]
    image_png_bytes: bytes
    width_pt: float
    height_pt: float


@dataclass
class FloatingTextBox:
    """Isolated / floating stamp or header tag rendered as an independent Word TextBox / Frame."""

    bbox: tuple[float, float, float, float]
    text: str
    font_size_pt: float = 12.0
    is_bold: bool = False
    is_italic: bool = False
    alignment: str = "CENTER"
    has_border: bool = False


class FloatingTextBoxDetector:
    """Detects floating administrative stamps/tags (e.g., MẬT, KHẨN, BẢN SỐ) and isolated box labels."""

    @staticmethod
    def classify_segment(
        seg: LogicalLine,
        row: Sequence[LogicalLine],
        all_rows: Sequence[Sequence[LogicalLine]],
        page_width_pt: float,
        content_bounds: tuple[float, float, float, float],
    ) -> FloatingTextBox | None:
        """Classify isolated administrative stamps (`MẬT`, `KHẨN`) and floating `BẢN SỐ:...` boxes."""
        txt = seg.text.strip()
        if not txt:
            return None

        if _CONFIDENTIAL_OR_URGENT_STAMP_RE.match(txt):
            return FloatingTextBox(
                bbox=seg.bbox,
                text=txt.upper(),
                font_size_pt=13.0,
                is_bold=True,
                is_italic=False,
                alignment="CENTER",
                has_border=True,
            )

        if _FLOATING_ADMIN_TAG_RE.match(txt) and txt.upper().startswith("BẢN SỐ"):
            return FloatingTextBox(
                bbox=seg.bbox,
                text=txt,
                font_size_pt=11.0,
                is_bold=False,
                is_italic=False,
                alignment="CENTER",
                has_border=False,
            )

        return None


BodyElement = Union[SynthesizedParagraph, ParallelColumnsBlock, ReconstructedTable, FigureBlock, FloatingTextBox]


@dataclass
class ADRTPageDocument:
    """Complete 6-layer reconstructed representation of a single document page."""

    page_index: int
    geometry: PageGeometry
    header_paragraphs: list[SynthesizedParagraph] = field(default_factory=list)
    footer_paragraphs: list[SynthesizedParagraph] = field(default_factory=list)
    body_elements: list[BodyElement] = field(default_factory=list)
    tables: list[ReconstructedTable] = field(default_factory=list)
    figures: list[FigureBlock] = field(default_factory=list)


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def snap_font_size_pt(raw_pt: float) -> float:
    """Snap an estimated box point height to the nearest standard Word font size."""
    return min(STANDARD_WORD_FONT_SIZES, key=lambda s: abs(s - raw_pt))


def _bbox_center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    return (0.5 * (bbox[0] + bbox[2]), 0.5 * (bbox[1] + bbox[3]))


def _vertical_overlap_ratio(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    ha = max(1e-3, a[3] - a[1])
    hb = max(1e-3, b[3] - b[1])
    inter = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return inter / min(ha, hb)


def _cluster_coords(coords: Sequence[float], tolerance: float = 4.5) -> list[float]:
    """Merge sorted 1D coordinates within `tolerance` into cluster means."""
    if not coords:
        return []
    ordered = sorted(coords)
    clusters: list[list[float]] = [[ordered[0]]]
    for c in ordered[1:]:
        if c - clusters[-1][-1] <= tolerance:
            clusters[-1].append(c)
        else:
            clusters.append([c])
    return [float(sum(cl) / len(cl)) for cl in clusters]


def _compute_crop_stroke_metrics(
    image_bgr: np.ndarray,
    bbox_px: tuple[float, float, float, float],
) -> tuple[float, float]:
    """Return `(mean_stroke_radius_px, ink_ratio)` for bold weight inference."""
    img_h, img_w = image_bgr.shape[:2]
    x0 = max(0, int(round(bbox_px[0])))
    y0 = max(0, int(round(bbox_px[1])))
    x1 = min(img_w, int(round(bbox_px[2])))
    y1 = min(img_h, int(round(bbox_px[3])))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return 0.0, 0.0

    crop = image_bgr[y0:y1, x0:x1]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    # Otsu inverse thresholding yields foreground text pixels = 255
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    ys, xs = np.where(binary > 0)
    if len(xs) < 6:
        return 0.0, 0.0

    tight = binary[int(ys.min()) : int(ys.max()) + 1, int(xs.min()) : int(xs.max()) + 1]
    ink_ratio = float(np.mean(tight > 0))
    dist = cv2.distanceTransform(tight, cv2.DIST_L2, 3)
    ink_dists = dist[tight > 0]
    mean_radius = float(np.mean(ink_dists)) if ink_dists.size > 0 else 0.0
    return mean_radius, ink_ratio


def should_merge_into_paragraph(
    curr_line: LogicalLine,
    next_line: LogicalLine,
    block_bounds: tuple[float, float, float, float],
    median_line_height: float,
) -> bool:
    """Decide whether `next_line` is a soft-wrapped continuation of `curr_line`.

    Implements ADRT 5-condition anti-hard-return logic:
    1. Same block & compatible style (boldness).
    2. Font height difference <= 20%.
    3. Vertical gap <= 1.45 * median_line_height.
    4. `curr_line` reaches near block right boundary (x_max >= block_right - 0.18 * block_width).
    5. `next_line` does NOT start with a Vietnamese list/clause marker or a new first-line indent.
    """
    block_left, _block_top, block_right, _block_bottom = block_bounds
    block_width = max(1.0, block_right - block_left)

    curr_h = max(1.0, curr_line.bbox[3] - curr_line.bbox[1])
    next_h = max(1.0, next_line.bbox[3] - next_line.bbox[1])

    curr_stripped = curr_line.text.strip()
    next_stripped = next_line.text.strip()
    neither_all_caps = (not curr_stripped.isupper()) and (not next_stripped.isupper())
    continues_sentence = bool(curr_stripped) and curr_stripped[-1] not in ".:;!?"
    next_starts_continuation = bool(next_stripped) and (
        next_stripped[0].islower()
        or next_stripped[0] == "("
        or bool(re.match(r"^(?:19|20)\d{2}\b", next_stripped))
    )
    is_mid_sentence_wrap = neither_all_caps and continues_sentence and next_starts_continuation
    is_list_next = bool(_VIET_LIST_OR_CLAUSE_RE.match(next_stripped)) and not bool(
        re.match(r"^(?:19|20)\d{2}\s*[,;]", next_stripped)
    )
    is_title_line = bool(_DOC_TITLE_RE.match(curr_stripped)) or bool(_DOC_TITLE_RE.match(next_stripped))

    # Condition 5a: Do not merge when next_line starts a list/clause marker
    # (unless next_line is a 4-digit year continuation like "2026, cụ thể như sau:")
    if is_list_next:
        return False

    # Condition 5b: Do not merge centered standalone lines (titles/headings/captions/subtitles)
    block_center = 0.5 * (block_left + block_right)
    curr_center = 0.5 * (curr_line.bbox[0] + curr_line.bbox[2])
    curr_span = curr_line.bbox[2] - curr_line.bbox[0]
    if (
        curr_span < 0.72 * block_width
        and abs(curr_center - block_center) <= max(0.05 * block_width, 12.0)
        and (curr_line.bbox[0] - block_left) >= 18.0
        and (block_right - curr_line.bbox[2]) >= 18.0
    ):
        return False

    # Condition 3 values needed by orphan-tail check and later geometric checks
    ref_h = max(median_line_height, 0.5 * (curr_h + next_h), 6.0)
    delta_y = next_line.bbox[1] - curr_line.bbox[3]
    curr_yc = 0.5 * (curr_line.bbox[1] + curr_line.bbox[3])
    next_yc = 0.5 * (next_line.bbox[1] + next_line.bbox[3])

    # Orphan tail / mid-sentence continuation rule: if curr_line reaches near right margin,
    # has no sentence-final punctuation, and next_line is a non-list, non-indented continuation, merge unconditionally.
    if (
        neither_all_caps
        and continues_sentence
        and not is_title_line
        and next_yc > curr_yc
        and -0.75 * ref_h <= delta_y <= 2.2 * ref_h
        and curr_line.bbox[2] >= block_right - 0.32 * block_width
        and (next_line.bbox[0] - curr_line.bbox[0]) <= 14.0
    ):
        return True

    # Condition 1 & 2: Style compatibility & <= 20% height difference (with noisy OCR jitter tolerance)
    if curr_line.is_bold != next_line.is_bold:
        max_font_diff = 3.0 if is_mid_sentence_wrap else 1.5
        font_close = abs(curr_line.font_size_pt - next_line.font_size_pt) <= max_font_diff
        if not (font_close and neither_all_caps and continues_sentence):
            return False

    h_diff_ratio = abs(curr_h - next_h) / max(curr_h, next_h)
    max_h_ratio = 0.46 if is_mid_sentence_wrap else 0.36
    if h_diff_ratio > max_h_ratio or (
        not is_mid_sentence_wrap
        and h_diff_ratio > 0.22
        and abs(curr_line.font_size_pt - next_line.font_size_pt) > 1.5
    ):
        return False

    # Do not merge a completed bullet ending with sentence period into a new capitalized line
    if (
        curr_stripped.startswith(("-", "+", "*"))
        and curr_stripped.endswith(".")
        and next_stripped
        and next_stripped[0].isupper()
    ):
        return False

    # Condition 3: Vertical line gap (permit slightly overlapping unclipped boxes if center advances)
    max_delta_y = (1.65 if is_mid_sentence_wrap else 1.45) * ref_h
    if delta_y > max_delta_y or delta_y < -0.75 * ref_h or next_yc <= curr_yc:
        return False

    # Condition 5c: Do not merge when next_line introduces a new positive first-line indent
    # (e.g., previous paragraph ended near margin, next paragraph indents inward).
    if (next_line.bbox[0] - curr_line.bbox[0]) > 10.0 and (curr_line.bbox[0] - block_left) <= 12.0:
        return False

    # Condition 4: Previous line reaches near block right boundary
    right_margin_tol = 0.30 if is_mid_sentence_wrap else 0.22
    right_threshold = block_right - right_margin_tol * block_width
    if curr_line.bbox[2] < right_threshold:
        return False

    return True


def _extract_table_line_segments(
    image_bgr: np.ndarray,
    dpi: int,
    fitz_page: fitz.Page | None = None,
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    """Extract horizontal (y, x0, x1) and vertical (x, y0, y1) line segments in PDF points."""
    scale = 72.0 / float(dpi)
    h_segments: list[tuple[float, float, float]] = []
    v_segments: list[tuple[float, float, float]] = []

    # 1. Vector lines from PDF drawings if present
    if fitz_page is not None:
        try:
            page_w = float(fitz_page.rect.width)
            page_h = float(fitz_page.rect.height)
            for drawing in fitz_page.get_drawings():
                for item in drawing.get("items", []):
                    op = item[0]
                    if op == "l":
                        p1, p2 = item[1], item[2]
                        dx = abs(p2.x - p1.x)
                        dy = abs(p2.y - p1.y)
                        if dy <= 2.5 and dx >= 20.0 and dx < page_w * 0.96:
                            h_segments.append((0.5 * (p1.y + p2.y), min(p1.x, p2.x), max(p1.x, p2.x)))
                        elif dx <= 2.5 and dy >= 10.0 and dy < page_h * 0.95:
                            v_segments.append((0.5 * (p1.x + p2.x), min(p1.y, p2.y), max(p1.y, p2.y)))
                    elif op == "re":
                        rect = item[1]
                        rw = float(rect.width)
                        rh = float(rect.height)
                        if 20.0 <= rw < page_w * 0.96 and 10.0 <= rh < page_h * 0.95:
                            h_segments.append((float(rect.y0), float(rect.x0), float(rect.x1)))
                            h_segments.append((float(rect.y1), float(rect.x0), float(rect.x1)))
                            v_segments.append((float(rect.x0), float(rect.y0), float(rect.y1)))
                            v_segments.append((float(rect.x1), float(rect.y0), float(rect.y1)))
        except Exception:
            pass

    # 2. Morphological line detection from raster image
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    binary = cv2.adaptiveThreshold(~gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 15, -2)
    img_h, img_w = binary.shape

    horiz_len = max(24, img_w // 26)
    vert_len = max(18, img_h // 38)
    horiz_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (horiz_len, 1))
    vert_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, vert_len))

    horiz_mask = cv2.dilate(cv2.erode(binary, horiz_kernel, iterations=1), horiz_kernel, iterations=1)
    vert_mask = cv2.dilate(cv2.erode(binary, vert_kernel, iterations=1), vert_kernel, iterations=1)

    num_h, _lbl_h, stats_h, _ = cv2.connectedComponentsWithStats(horiz_mask)
    for i in range(1, num_h):
        x, y, w, h, _area = stats_h[i]
        if w >= horiz_len and h <= max(10, int(round(6.0 / scale))) and w < img_w * 0.96:
            y_mid = (y + 0.5 * h) * scale
            h_segments.append((y_mid, x * scale, (x + w) * scale))

    num_v, _lbl_v, stats_v, _ = cv2.connectedComponentsWithStats(vert_mask)
    for i in range(1, num_v):
        x, y, w, h, _area = stats_v[i]
        if h >= vert_len and w <= max(10, int(round(6.0 / scale))) and h < img_h * 0.95:
            x_mid = (x + 0.5 * w) * scale
            v_segments.append((x_mid, y * scale, (y + h) * scale))

    return h_segments, v_segments


class ADRTLayoutEngine:
    """5-layer ADRT-style geometric document layout & typography analyzer."""

    def __init__(self, dpi: int = 300) -> None:
        self.dpi = dpi

    def detect_bordered_tables(
        self,
        image_bgr: np.ndarray,
        fitz_page: fitz.Page | None = None,
    ) -> list[dict]:
        """Layer 2a: Reconstruct table grids and detect merged cells (`row_span`, `col_span`)."""
        h_segs, v_segs = _extract_table_line_segments(image_bgr, self.dpi, fitz_page=fitz_page)
        if len(h_segs) < 3 or len(v_segs) < 3:
            return []

        tol = 5.0
        # Build intersection adjacency between H and V segments
        h_adj: list[list[int]] = [[] for _ in h_segs]
        v_adj: list[list[int]] = [[] for _ in v_segs]
        for hi, (hy, hx0, hx1) in enumerate(h_segs):
            for vi, (vx, vy0, vy1) in enumerate(v_segs):
                if (hx0 - tol) <= vx <= (hx1 + tol) and (vy0 - tol) <= hy <= (vy1 + tol):
                    h_adj[hi].append(vi)
                    v_adj[vi].append(hi)

        visited_h = [False] * len(h_segs)
        visited_v = [False] * len(v_segs)
        raw_tables: list[dict] = []

        for start_h in range(len(h_segs)):
            if visited_h[start_h] or not h_adj[start_h]:
                continue
            comp_h: list[int] = []
            comp_v: list[int] = []
            queue_h = [start_h]
            visited_h[start_h] = True
            while queue_h:
                cur_h = queue_h.pop()
                comp_h.append(cur_h)
                for vi in h_adj[cur_h]:
                    if not visited_v[vi]:
                        visited_v[vi] = True
                        comp_v.append(vi)
                        for nh in v_adj[vi]:
                            if not visited_h[nh]:
                                visited_h[nh] = True
                                queue_h.append(nh)

            if len(comp_h) < 3 or len(comp_v) < 3:
                continue

            sub_h = [h_segs[i] for i in comp_h]
            sub_v = [v_segs[i] for i in comp_v]

            ys = _cluster_coords([s[0] for s in sub_h], tolerance=4.5)
            xs = _cluster_coords([s[0] for s in sub_v], tolerance=4.5)
            if len(ys) < 3 or len(xs) < 3:
                continue

            # Ignore tables that occupy negligible size
            tbl_w = xs[-1] - xs[0]
            tbl_h = ys[-1] - ys[0]
            if tbl_w < 60.0 or tbl_h < 30.0:
                continue

            num_rows = len(ys) - 1
            num_cols = len(xs) - 1

            def has_v_wall(col_line_idx: int, row_idx: int) -> bool:
                target_x = xs[col_line_idx]
                ry0, ry1 = ys[row_idx], ys[row_idx + 1]
                row_len = max(1.0, ry1 - ry0)
                covered = 0.0
                for vx, vy0, vy1 in sub_v:
                    if abs(vx - target_x) <= 5.5:
                        covered += max(0.0, min(vy1, ry1) - max(vy0, ry0))
                return (covered / row_len) >= 0.52

            def has_h_wall(row_line_idx: int, col_idx: int) -> bool:
                target_y = ys[row_line_idx]
                cx0, cx1 = xs[col_idx], xs[col_idx + 1]
                col_len = max(1.0, cx1 - cx0)
                covered = 0.0
                for hy, hx0, hx1 in sub_h:
                    if abs(hy - target_y) <= 5.5:
                        covered += max(0.0, min(hx1, cx1) - max(hx0, cx0))
                return (covered / col_len) >= 0.52

            occupied = [[False] * num_cols for _ in range(num_rows)]
            grid_cells: list[dict] = []

            for r in range(num_rows):
                for c in range(num_cols):
                    if occupied[r][c]:
                        continue

                    col_span = 1
                    while (c + col_span) < num_cols and not occupied[r][c + col_span]:
                        if has_v_wall(c + col_span, r):
                            break
                        col_span += 1

                    row_span = 1
                    while (r + row_span) < num_rows:
                        next_r = r + row_span
                        if any(occupied[next_r][cc] for cc in range(c, c + col_span)):
                            break
                        if any(has_h_wall(next_r, cc) for cc in range(c, c + col_span)):
                            break
                        if col_span > 1 and any(has_v_wall(cc, next_r) for cc in range(c + 1, c + col_span)):
                            break
                        row_span += 1

                    for rr in range(r, r + row_span):
                        for cc in range(c, c + col_span):
                            occupied[rr][cc] = True

                    grid_cells.append(
                        {
                            "row": r,
                            "col": c,
                            "row_span": row_span,
                            "col_span": col_span,
                            "bbox": (
                                float(xs[c]),
                                float(ys[r]),
                                float(xs[c + col_span]),
                                float(ys[r + row_span]),
                            ),
                        }
                    )

            raw_tables.append(
                {
                    "bbox": (float(xs[0]), float(ys[0]), float(xs[-1]), float(ys[-1])),
                    "rows_count": num_rows,
                    "cols_count": num_cols,
                    "col_widths_pt": [float(xs[i + 1] - xs[i]) for i in range(num_cols)],
                    "row_heights_pt": [float(ys[i + 1] - ys[i]) for i in range(num_rows)],
                    "cells": grid_cells,
                }
            )

        raw_tables.sort(key=lambda t: (t["bbox"][1], t["bbox"][0]))
        return raw_tables

    def extract_figures(
        self,
        image_bgr: np.ndarray,
        page_w_pt: float,
        page_h_pt: float,
        table_bboxes_pt: Sequence[tuple[float, float, float, float]],
        ocr_bboxes_pt: Sequence[tuple[float, float, float, float]],
        fitz_page: fitz.Page | None = None,
    ) -> list[FigureBlock]:
        """Layer 2b: Extract non-text embedded figures/illustrations at their page position."""
        scale = 72.0 / float(self.dpi)
        img_h, img_w = image_bgr.shape[:2]
        page_area = max(1.0, page_w_pt * page_h_pt)
        figures: list[FigureBlock] = []

        def overlaps_any(box: tuple[float, float, float, float], candidates: Sequence[tuple[float, float, float, float]]) -> bool:
            bx0, by0, bx1, by1 = box
            b_area = max(1.0, (bx1 - bx0) * (by1 - by0))
            for cx0, cy0, cx1, cy1 in candidates:
                ix = max(0.0, min(bx1, cx1) - max(bx0, cx0))
                iy = max(0.0, min(by1, cy1) - max(by0, cy0))
                if (ix * iy) / b_area > 0.15:
                    return True
            return False

        # Prefer embedded image regions reported by PyMuPDF when not a full-page scan
        if fitz_page is not None:
            try:
                for info in fitz_page.get_image_info(xrefs=False):
                    bbox = tuple(float(v) for v in info["bbox"])
                    bw = bbox[2] - bbox[0]
                    bh = bbox[3] - bbox[1]
                    area_ratio = (bw * bh) / page_area
                    if bw < 24.0 or bh < 24.0 or area_ratio >= 0.65:
                        continue
                    if overlaps_any(bbox, table_bboxes_pt) or overlaps_any(bbox, ocr_bboxes_pt):
                        continue
                    px0 = max(0, int(round(bbox[0] / scale)))
                    py0 = max(0, int(round(bbox[1] / scale)))
                    px1 = min(img_w, int(round(bbox[2] / scale)))
                    py1 = min(img_h, int(round(bbox[3] / scale)))
                    if px1 - px0 < 12 or py1 - py0 < 12:
                        continue
                    crop = image_bgr[py0:py1, px0:px1]
                    ok, encoded = cv2.imencode(".png", crop)
                    if ok:
                        figures.append(
                            FigureBlock(
                                bbox=bbox,
                                image_png_bytes=encoded.tobytes(),
                                width_pt=bw,
                                height_pt=bh,
                            )
                        )
            except Exception:
                pass

        if figures:
            figures.sort(key=lambda f: (f.bbox[1], f.bbox[0]))
            return figures

        # Fallback: detect non-text graphic blobs on raster image outside text/table zones
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        nonwhite = (gray < 238).astype(np.uint8) * 255
        # Mask out OCR boxes & tables
        for x0, y0, x1, y1 in list(table_bboxes_pt) + list(ocr_bboxes_pt):
            px0 = max(0, int(round(x0 / scale)) - 4)
            py0 = max(0, int(round(y0 / scale)) - 4)
            px1 = min(img_w, int(round(x1 / scale)) + 4)
            py1 = min(img_h, int(round(y1 / scale)) + 4)
            nonwhite[py0:py1, px0:px1] = 0

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
        closed = cv2.morphologyEx(nonwhite, cv2.MORPH_CLOSE, kernel)
        num_labels, _labels, stats, _ = cv2.connectedComponentsWithStats(closed)
        for i in range(1, num_labels):
            x, y, w, h, area = stats[i]
            bw = w * scale
            bh = h * scale
            area_ratio = (bw * bh) / page_area
            aspect = max(bw / max(1.0, bh), bh / max(1.0, bw))
            if bw < 32.0 or bh < 32.0 or aspect > 8.0:
                continue
            if not (0.008 <= area_ratio <= 0.45):
                continue
            if area < (w * h * 0.08):
                continue
            bbox = (x * scale, y * scale, (x + w) * scale, (y + h) * scale)
            if overlaps_any(bbox, table_bboxes_pt) or overlaps_any(bbox, ocr_bboxes_pt):
                continue
            crop = image_bgr[y : y + h, x : x + w]
            ok, encoded = cv2.imencode(".png", crop)
            if ok:
                figures.append(
                    FigureBlock(
                        bbox=bbox,
                        image_png_bytes=encoded.tobytes(),
                        width_pt=bw,
                        height_pt=bh,
                    )
                )

        figures.sort(key=lambda f: (f.bbox[1], f.bbox[0]))
        return figures

    def _build_word_spans(
        self,
        detections: Sequence[tuple[tuple[float, float, float, float], str]],
        image_bgr: np.ndarray,
    ) -> tuple[list[WordSpan], float]:
        """Convert raw pixel OCR detections into calibrated `WordSpan`s + body point size."""
        scale = 72.0 / float(self.dpi)
        if not detections:
            return [], 11.0

        raw_pts: list[float] = []
        raw_items: list[tuple[tuple[float, float, float, float], str, float, float, float]] = []

        for box_px, text in detections:
            clean = normalize_vietnamese_nfc(text, collapse_spaces=True).strip()
            if not clean:
                continue
            rel_yc = (0.5 * (box_px[1] + box_px[3])) / max(1.0, float(image_bgr.shape[0]))
            if is_browser_or_viewer_noise(clean, rel_yc=rel_yc):
                continue
            x0, y0, x1, y1 = (v * scale for v in box_px)
            box_h_pt = max(6.0, y1 - y0)
            est_pt = _clamp(box_h_pt * 0.80, 8.0, 28.0)
            mean_r, ink_ratio = _compute_crop_stroke_metrics(image_bgr, box_px)
            raw_pts.append(est_pt)
            raw_items.append(((x0, y0, x1, y1), clean, est_pt, mean_r, ink_ratio))

        if not raw_items:
            return [], 11.0

        # Normalize modal body height onto ~11pt/12pt range so PaddleOCR box padding doesn't inflate sizes
        median_raw_pt = statistics.median(raw_pts)
        scale_adjust = 1.0
        if 12.2 <= median_raw_pt <= 16.5:
            scale_adjust = 11.5 / median_raw_pt
        body_pt = snap_font_size_pt(_clamp(median_raw_pt * scale_adjust, 9.5, 13.0))

        # Estimate regular-weight body baselines from lower-half / modal stroke metrics
        body_r_vals = [
            mr / max(0.75, snap_font_size_pt(_clamp(ep * scale_adjust, 8.0, 28.0)) / body_pt)
            for _, cl, ep, mr, _ in raw_items
            if mr > 0.0 and len(cl) >= 4
        ]
        body_ink_vals = [ir for _, cl, _, mr, ir in raw_items if mr > 0.0 and len(cl) >= 4]
        median_r = statistics.median(body_r_vals) if body_r_vals else 1.2
        median_ink = statistics.median(body_ink_vals) if body_ink_vals else 0.17

        spans: list[WordSpan] = []
        for bbox, clean, est_pt, mean_r, ink_ratio in raw_items:
            snapped = snap_font_size_pt(_clamp(est_pt * scale_adjust, 8.0, 28.0))
            is_bold = False
            score = 0.0
            if mean_r > 0.0 and median_r > 0.0 and median_ink > 0.0:
                size_factor = max(0.75, snapped / body_pt)
                rel_r = (mean_r / size_factor) / median_r
                rel_ink = ink_ratio / median_ink
                score = 0.70 * rel_r + 0.30 * rel_ink
                if len(clean) < 4:
                    is_bold = rel_r >= 1.22
                else:
                    is_bold = score >= 1.15 or (snapped >= body_pt * 1.18 and score >= 1.05)

            spans.append(
                WordSpan(
                    bbox=bbox,
                    text=clean,
                    font_size_pt=snapped,
                    is_bold=is_bold,
                    stroke_score=score,
                )
            )
        return spans, body_pt

    def group_logical_lines(
        self,
        spans: Sequence[WordSpan],
        page_width_pt: float,
    ) -> list[list[LogicalLine]]:
        """Layer 3a: Group WordSpans into horizontal rows, splitting wide column gutters into separate segments."""
        if not spans:
            return []

        ordered = sorted(spans, key=lambda s: (0.5 * (s.bbox[1] + s.bbox[3]), s.bbox[0]))
        rows: list[list[WordSpan]] = []
        for sp in ordered:
            sp_yc = 0.5 * (sp.bbox[1] + sp.bbox[3])
            sp_h = max(1.0, sp.bbox[3] - sp.bbox[1])
            sp_w = max(1.0, sp.bbox[2] - sp.bbox[0])
            placed = False
            for row in rows:
                # Never merge vertically stacked spans with >25% horizontal X-overlap into the same row
                x_conflicts = False
                for r_sp in row:
                    r_w = max(1.0, r_sp.bbox[2] - r_sp.bbox[0])
                    x_ov = max(0.0, min(sp.bbox[2], r_sp.bbox[2]) - max(sp.bbox[0], r_sp.bbox[0]))
                    if x_ov / min(sp_w, r_w) > 0.25:
                        x_conflicts = True
                        break
                if x_conflicts:
                    continue

                row_yc = statistics.mean(0.5 * (s.bbox[1] + s.bbox[3]) for s in row)
                row_h = max(1.0, statistics.mean(s.bbox[3] - s.bbox[1] for s in row))
                rep = row[0]
                min_h = min(sp_h, row_h)
                if abs(sp_yc - row_yc) <= 0.45 * min_h or _vertical_overlap_ratio(sp.bbox, rep.bbox) >= 0.50:
                    row.append(sp)
                    placed = True
                    break
            if not placed:
                rows.append([sp])

        rows.sort(key=lambda r: statistics.mean(0.5 * (s.bbox[1] + s.bbox[3]) for s in r))

        line_rows: list[list[LogicalLine]] = []
        # Any horizontal gap exceeding max(2.5 * avg_char_w, 16pt) or 10% of page width splits column segments
        max_gutter_pt = 0.10 * page_width_pt

        for row in rows:
            row.sort(key=lambda s: s.bbox[0])
            segments: list[list[WordSpan]] = [[row[0]]]
            for sp in row[1:]:
                prev = segments[-1][-1]
                gap_x = sp.bbox[0] - prev.bbox[2]
                total_chars = max(2, len(prev.text) + len(sp.text))
                total_w = max(8.0, (prev.bbox[2] - prev.bbox[0]) + (sp.bbox[2] - sp.bbox[0]))
                avg_char_w = total_w / total_chars
                merge_limit = min(max(2.5 * avg_char_w, 14.0), max_gutter_pt)
                if gap_x <= merge_limit:
                    segments[-1].append(sp)
                else:
                    segments.append([sp])

            logical_row: list[LogicalLine] = []
            for seg in segments:
                bx0 = min(s.bbox[0] for s in seg)
                by0 = min(s.bbox[1] for s in seg)
                bx1 = max(s.bbox[2] for s in seg)
                by1 = max(s.bbox[3] for s in seg)
                raw_merged = normalize_vietnamese_nfc(" ".join(s.text for s in seg), collapse_spaces=True)
                merged_text = VietnameseAdminPostProcessor.clean_line(raw_merged)
                if not merged_text or is_browser_or_viewer_noise(merged_text):
                    continue
                sizes = [s.font_size_pt for s in seg]
                font_pt = snap_font_size_pt(statistics.median(sizes))
                # Weighted bold majority by text length
                bold_chars = sum(len(s.text) for s in seg if s.is_bold)
                total_seg_chars = max(1, sum(len(s.text) for s in seg))
                seg_bold = (bold_chars / total_seg_chars) >= 0.5
                logical_row.append(
                    LogicalLine(
                        bbox=(bx0, by0, bx1, by1),
                        text=merged_text,
                        words=list(seg),
                        font_size_pt=font_pt,
                        is_bold=seg_bold,
                        baseline_y=by1,
                    )
                )

            if not logical_row:
                continue

            # Split inline merged sub-items (e.g. "1.1. ... 1.2. ...") into separate stacked rows
            if len(logical_row) == 1:
                only = logical_row[0]
                sub_parts = VietnameseAdminPostProcessor.split_inline_subitems(only.text)
                if len(sub_parts) > 1:
                    bx0, by0, bx1, by1 = only.bbox
                    step_y = max(1.0, (by1 - by0) / len(sub_parts))
                    for k, sub_txt in enumerate(sub_parts):
                        sy0 = by0 + k * step_y
                        sy1 = sy0 + max(8.0, step_y)
                        line_rows.append(
                            [
                                LogicalLine(
                                    bbox=(bx0, sy0, bx1, sy1),
                                    text=sub_txt,
                                    words=list(only.words),
                                    font_size_pt=only.font_size_pt,
                                    is_bold=only.is_bold,
                                    baseline_y=sy1,
                                )
                            ]
                        )
                    continue

            line_rows.append(logical_row)

        return line_rows

    def _synthesize_paragraphs_from_lines(
        self,
        lines: Sequence[LogicalLine],
        block_bounds: tuple[float, float, float, float],
        body_pt: float,
        prev_bottom_y: float | None = None,
        allow_center_sibling_propagation: bool = False,
    ) -> list[SynthesizedParagraph]:
        """Layers 4 & 5: Merge `LogicalLine`s into paragraphs and infer alignment, indents & spacing."""
        if not lines:
            return []

        ordered = sorted(lines, key=lambda l: (0.5 * (l.bbox[1] + l.bbox[3]), l.bbox[0]))
        heights = [max(4.0, l.bbox[3] - l.bbox[1]) for l in ordered]
        median_h = statistics.median(heights) if heights else 12.0

        groups: list[list[LogicalLine]] = [[ordered[0]]]
        for nxt in ordered[1:]:
            curr = groups[-1][-1]
            if should_merge_into_paragraph(curr, nxt, block_bounds, median_h):
                groups[-1].append(nxt)
            else:
                groups.append([nxt])

        block_left, _block_top, block_right, _block_bottom = block_bounds
        block_width = max(1.0, block_right - block_left)
        block_center = 0.5 * (block_left + block_right)

        paragraphs: list[SynthesizedParagraph] = []
        last_y1 = prev_bottom_y

        for g_lines in groups:
            px0 = min(l.bbox[0] for l in g_lines)
            py0 = min(l.bbox[1] for l in g_lines)
            px1 = max(l.bbox[2] for l in g_lines)
            py1 = max(l.bbox[3] for l in g_lines)

            text = VietnameseAdminPostProcessor.clean_line(repair_line_wraps(l.text for l in g_lines))
            font_pt = snap_font_size_pt(statistics.median(l.font_size_pt for l in g_lines))
            is_bold = sum(1 for l in g_lines if l.is_bold) > (len(g_lines) / 2.0)

            # Layer 5: Alignment inference
            alignment = "LEFT"
            is_list = bool(_VIET_LIST_OR_CLAUSE_RE.match(text))

            para_center = 0.5 * (px0 + px1)
            left_inset = px0 - block_left
            right_inset = block_right - px1

            if len(g_lines) >= 2:
                # Multi-line paragraph alignment
                cont_lines = g_lines[:-1]
                reaches_right = all(l.bbox[2] >= block_right - 0.16 * block_width for l in cont_lines)
                starts_left = all((l.bbox[0] - block_left) <= 0.14 * block_width for l in g_lines[1:])
                centers_aligned = all(
                    abs(0.5 * (l.bbox[0] + l.bbox[2]) - block_center) <= max(0.045 * block_width, 12.0)
                    for l in g_lines
                )
                symmetric_insets = all(
                    abs((l.bbox[0] - block_left) - (block_right - l.bbox[2])) <= max(0.08 * block_width, 20.0)
                    and (l.bbox[0] - block_left) >= 8.0
                    for l in g_lines
                )
                if centers_aligned and (symmetric_insets or (allow_center_sibling_propagation and not reaches_right)):
                    alignment = "CENTER"
                elif reaches_right and starts_left and not is_list:
                    alignment = "JUSTIFY"
                elif reaches_right and starts_left and is_list and block_width >= 200.0:
                    alignment = "JUSTIFY"
                elif (block_right - px1) <= 0.08 * block_width and min(l.bbox[0] - block_left for l in g_lines) > 0.45 * block_width:
                    alignment = "RIGHT"
            else:
                # Single-line paragraph alignment
                center_diff = abs(para_center - block_center)
                inset_diff = abs(left_inset - right_inset)
                is_full_cell_span = left_inset <= 6.0 and right_inset <= 6.0
                if not is_list and center_diff <= max(0.04 * block_width, 12.0):
                    if (
                        inset_diff <= max(0.08 * block_width, 20.0)
                        and left_inset >= 8.0
                        and (allow_center_sibling_propagation or right_inset >= 12.0)
                    ) or (
                        allow_center_sibling_propagation and not is_full_cell_span
                    ):
                        alignment = "CENTER"
                    elif (
                        is_bold
                        and font_pt >= body_pt * 1.15
                        and inset_diff <= max(0.10 * block_width, 24.0)
                        and (allow_center_sibling_propagation or right_inset >= 12.0)
                    ):
                        alignment = "CENTER"
                if _DOC_TITLE_RE.match(text):
                    alignment = "CENTER"
                    is_bold = True
                if alignment == "LEFT" and right_inset <= 0.08 * block_width and left_inset > 0.45 * block_width:
                    alignment = "RIGHT"

            # Indentation inference
            left_indent_pt = 0.0
            first_line_indent_pt = 0.0
            if alignment in ("LEFT", "JUSTIFY"):
                if len(g_lines) >= 2:
                    cont_x0 = min(l.bbox[0] for l in g_lines[1:])
                    raw_left_ind = max(0.0, cont_x0 - block_left)
                    left_indent_pt = round(raw_left_ind, 1) if raw_left_ind >= 8.0 else 0.0
                    indent_jump = g_lines[0].bbox[0] - cont_x0
                    if indent_jump >= 8.0:
                        first_line_indent_pt = round(_clamp(indent_jump, 10.0, 48.0), 1)
                else:
                    if 10.0 <= left_inset <= 48.0 and not is_list:
                        first_line_indent_pt = round(left_inset, 1)
                    elif left_inset > 48.0:
                        left_indent_pt = round(left_inset, 1)

            # Vertical spacing
            space_before_pt = 0.0
            if last_y1 is not None:
                v_gap = max(0.0, py0 - last_y1)
                space_before_pt = round(_clamp(v_gap - 0.35 * median_h, 0.0, 24.0), 1)
            last_y1 = py1

            # Heading detection
            is_heading = False
            heading_level = 0
            if len(text) <= 140 and not is_list:
                if font_pt >= body_pt * 1.45:
                    is_heading = True
                    heading_level = 1
                elif font_pt >= body_pt * 1.18 and (is_bold or alignment == "CENTER") and not text.endswith("."):
                    is_heading = True
                    heading_level = 2

            paragraphs.append(
                SynthesizedParagraph(
                    bbox=(px0, py0, px1, py1),
                    text=text,
                    lines=list(g_lines),
                    alignment=alignment,
                    left_indent_pt=left_indent_pt,
                    right_indent_pt=0.0,
                    first_line_indent_pt=first_line_indent_pt,
                    space_before_pt=space_before_pt,
                    space_after_pt=6.0 if is_heading else 3.0,
                    line_spacing=1.15,
                    font_size_pt=font_pt,
                    is_bold=is_bold,
                    is_heading=is_heading,
                    heading_level=heading_level,
                    is_list_item=is_list,
                )
            )

        # If a parallel cell mixes short centered lines with a wide sibling line (like QUỐC HIỆU / TIÊU NGỮ),
        # keep the wide sibling centered with them so the cell aligns uniformly.
        if allow_center_sibling_propagation and len(paragraphs) >= 2:
            centered_count = sum(1 for p in paragraphs if p.alignment == "CENTER")
            if centered_count >= 1:
                for p in paragraphs:
                    if (
                        p.alignment == "LEFT"
                        and len(p.lines) == 1
                        and not p.is_list_item
                        and abs(0.5 * (p.bbox[0] + p.bbox[2]) - block_center) <= 0.12 * block_width
                        and (p.bbox[0] - block_left) <= 12.0
                        and (block_right - p.bbox[2]) <= 12.0
                    ):
                        p.alignment = "CENTER"
                        p.first_line_indent_pt = 0.0
                        p.left_indent_pt = 0.0

        return paragraphs

    @staticmethod
    def _style_parallel_block(block: ParallelColumnsBlock) -> None:
        """Apply Vietnamese administrative typography rules to Header and Footer 2-column tables."""
        all_texts = [p.text.strip() for p in (block.left_paragraphs + block.right_paragraphs)]
        joined_upper = "\n".join(all_texts).upper()

        is_admin_header = (
            "CỘNG HÒA" in joined_upper
            or "ĐỘC LẬP" in joined_upper
            or "HẠNH PHÚC" in joined_upper
            or any(t.upper().startswith(("SỐ:", "SỐ ", "SO:")) for t in all_texts)
            or any(t.upper().startswith(("CỤC ", "TRẠI ")) for t in all_texts)
        )
        if is_admin_header:
            for idx, lp in enumerate(block.left_paragraphs):
                lp.alignment = "CENTER"
                lp.first_line_indent_pt = 0.0
                lp.left_indent_pt = 0.0
                lp.space_before_pt = 0.0
                lp.space_after_pt = 2.0
                lp.line_spacing = 1.15
                u = lp.text.strip().upper()
                if u.startswith("SỐ") or u.startswith("SO:"):
                    lp.font_size_pt = 13.0
                    lp.is_bold = False
                    lp.is_italic = False
                    lp.has_bottom_border = False
                elif idx == 0 and len(block.left_paragraphs) >= 3:
                    lp.font_size_pt = 12.0
                    lp.is_bold = False
                    lp.is_italic = False
                    lp.has_bottom_border = False
                else:
                    lp.font_size_pt = 13.0
                    lp.is_bold = True
                    lp.is_italic = False
                    lp.has_bottom_border = True

            for rp in block.right_paragraphs:
                rp.alignment = "CENTER"
                rp.first_line_indent_pt = 0.0
                rp.left_indent_pt = 0.0
                rp.space_before_pt = 0.0
                rp.space_after_pt = 2.0
                rp.line_spacing = 1.15
                u = rp.text.strip().upper()
                if "CỘNG HÒA" in u or "VIỆT NAM" in u:
                    rp.font_size_pt = 12.0
                    rp.is_bold = True
                    rp.is_italic = False
                    rp.has_bottom_border = False
                elif "ĐỘC LẬP" in u or "HẠNH PHÚC" in u:
                    rp.font_size_pt = 13.0
                    rp.is_bold = True
                    rp.is_italic = False
                    rp.has_bottom_border = True
                elif "NGÀY" in u and "NĂM" in u:
                    rp.font_size_pt = 13.0
                    rp.is_bold = False
                    rp.is_italic = True
                    rp.has_bottom_border = False
                else:
                    rp.font_size_pt = 12.0
            return

        is_footer_sig = any(t.lower().startswith("nơi nhận") for t in all_texts) or any(
            _SIGNATURE_ROLE_RE.match(t) for t in all_texts
        )
        if is_footer_sig:
            for lp in block.left_paragraphs:
                lp.alignment = "LEFT"
                lp.first_line_indent_pt = 0.0
                lp.left_indent_pt = 0.0
                t_clean = lp.text.strip()
                if t_clean.lower().startswith("nơi nhận"):
                    lp.font_size_pt = 12.0
                    lp.is_bold = True
                    lp.is_italic = True
                    lp.space_after_pt = 2.0
                else:
                    if not t_clean.startswith("-"):
                        lp.text = f"- {t_clean}"
                    lp.font_size_pt = 11.0
                    lp.is_bold = False
                    lp.is_italic = False
                    lp.space_before_pt = 0.0
                    lp.space_after_pt = 1.5

            for rp in block.right_paragraphs:
                rp.alignment = "CENTER"
                rp.first_line_indent_pt = 0.0
                rp.left_indent_pt = 0.0
                t_clean = rp.text.strip()
                if _SIGNATURE_ROLE_RE.match(t_clean):
                    rp.font_size_pt = 13.0
                    rp.is_bold = True
                    rp.is_italic = False
                elif _is_signer_name_span(t_clean):
                    rp.font_size_pt = 14.0
                    rp.is_bold = True
                    rp.is_italic = False
                elif t_clean.startswith("("):
                    rp.font_size_pt = 11.0
                    rp.is_bold = False
                    rp.is_italic = True
                else:
                    rp.font_size_pt = 13.0
                    rp.is_bold = True

    @staticmethod
    def _style_body_paragraphs(
        elements: list[Union[SynthesizedParagraph, ParallelColumnsBlock, FloatingTextBox]],
    ) -> None:
        """Standardize typography scale, indents, and bold prefixes across document body elements."""
        expect_subtitle = False
        for el in elements:
            if isinstance(el, ParallelColumnsBlock):
                expect_subtitle = False
                continue
            if not isinstance(el, SynthesizedParagraph):
                continue

            txt = el.text.strip()
            if _DOC_TITLE_RE.match(txt):
                el.font_size_pt = 16.0
                el.is_bold = True
                el.is_italic = False
                el.is_heading = False
                el.heading_level = 0
                el.alignment = "CENTER"
                el.first_line_indent_pt = 0.0
                el.left_indent_pt = 0.0
                el.right_indent_pt = 0.0
                el.space_before_pt = 12.0
                el.space_after_pt = 4.0
                expect_subtitle = True
                continue

            if (
                expect_subtitle
                and not re.match(r"^(?:Thực\s+hiện|Căn\s+cứ|[IVXLCDM]{1,6}\.\s|\d+\.\s)", txt, re.IGNORECASE)
                and not txt.startswith("Hệ thống")
            ):
                el.font_size_pt = 14.0
                el.is_bold = True
                el.is_italic = False
                el.is_heading = False
                el.heading_level = 0
                el.alignment = "CENTER"
                el.left_indent_pt = 36.0
                el.right_indent_pt = 36.0
                el.first_line_indent_pt = 0.0
                el.space_before_pt = 2.0
                el.space_after_pt = 8.0
                el.has_bottom_border = True
                expect_subtitle = False
                continue

            expect_subtitle = False
            el.font_size_pt = 14.0
            el.line_spacing = 1.15
            el.is_heading = False
            el.heading_level = 0

            if re.match(r"^[IVXLCDM]{1,6}\.\s+", txt):
                el.is_bold = True
                el.alignment = "LEFT"
                el.first_line_indent_pt = 0.0
                el.left_indent_pt = 0.0
                el.space_before_pt = max(6.0, el.space_before_pt)
                el.space_after_pt = 3.0
            elif re.match(r"^\*\s*(?:Tình\s+huống|Biện\s+pháp\s+xử\s+lý)\b", txt, re.IGNORECASE):
                el.is_bold = True
                el.alignment = "LEFT"
                el.first_line_indent_pt = 0.0
                el.left_indent_pt = 0.0
            elif re.match(r"^\d{1,2}\.\d{1,2}\.?\s*Đợt\s+\d+\s*:\s*$", txt):
                el.is_bold = True
                el.alignment = "LEFT"
                el.first_line_indent_pt = 0.0
            elif m_sub := re.match(r"^(?P<prefix>\d{1,2}\.\d{1,2}\.?(?:\s*Đợt\s+\d+\s*:)?)\s+.+", txt):
                el.is_bold = False
                el.bold_prefix_len = len(m_sub.group("prefix"))
                el.first_line_indent_pt = 0.0
            elif m_num := re.match(r"^(?P<num>\d{1,2}\.)\s+(?P<rest>.+)$", txt):
                if len(txt) <= 55 and ":" not in txt and not txt.endswith("."):
                    el.is_bold = True
                    el.alignment = "LEFT"
                    el.first_line_indent_pt = 0.0
                elif m_lbl := re.match(r"^(?P<prefix>\d{1,2}\.\s+[^:]{2,25}:)\s+.+", txt):
                    el.is_bold = False
                    el.bold_prefix_len = len(m_lbl.group("prefix"))
                    el.alignment = "JUSTIFY"
                    el.first_line_indent_pt = 28.0
                else:
                    el.is_bold = False
                    el.bold_prefix_len = len(m_num.group("num"))
                    el.alignment = "JUSTIFY"
                    el.first_line_indent_pt = 28.0
            elif not el.is_list_item and el.alignment != "CENTER":
                el.is_bold = False
                el.alignment = "JUSTIFY"
                el.first_line_indent_pt = 28.0
                el.left_indent_pt = 0.0

    def _partition_body_bands(
        self,
        line_rows: list[list[LogicalLine]],
        content_bounds: tuple[float, float, float, float],
        page_width_pt: float,
        body_pt: float,
    ) -> list[Union[SynthesizedParagraph, ParallelColumnsBlock, FloatingTextBox]]:
        """Layer 3b: Split horizontal rows into normal full-width flow, 2-col blocks & floating TextBoxes."""
        if not line_rows:
            return []

        all_heights = [max(4.0, l.bbox[3] - l.bbox[1]) for row in line_rows for l in row]
        median_h = statistics.median(all_heights) if all_heights else 12.0

        def find_split_x(row: list[LogicalLine]) -> float | None:
            if len(row) < 2:
                return None
            # Find widest horizontal gap between left and right segments exceeding 8% of page width
            best_gap = 0.0
            best_mid = None
            for idx in range(len(row) - 1):
                gap = row[idx + 1].bbox[0] - row[idx].bbox[2]
                mid = 0.5 * (row[idx].bbox[2] + row[idx + 1].bbox[0])
                if gap > best_gap and 0.22 * page_width_pt <= mid <= 0.78 * page_width_pt:
                    best_gap = gap
                    best_mid = mid
            if best_gap >= max(0.08 * page_width_pt, 36.0):
                return best_mid
            return None

        # Pre-split mixed header-end rows like [MẬT, KẾ HOẠCH], extract floating stamps/tags, and drop bare page numbers
        floating_boxes: list[FloatingTextBox] = []
        filtered_rows: list[list[LogicalLine]] = []
        for row in line_rows:
            if len(row) == 1 and re.match(r"^\s*\d{1,2}\s*$", row[0].text):
                continue

            kept_row: list[LogicalLine] = []
            for seg in row:
                fbox = FloatingTextBoxDetector.classify_segment(
                    seg, row, line_rows, page_width_pt, content_bounds
                )
                if fbox is not None:
                    floating_boxes.append(fbox)
                else:
                    kept_row.append(seg)
            if not kept_row:
                continue

            if len(kept_row) >= 2 and _DOC_TITLE_RE.match(kept_row[-1].text):
                prefix = kept_row[:-1]
                title_seg = kept_row[-1]
                if prefix:
                    filtered_rows.append(prefix)
                filtered_rows.append([title_seg])
            else:
                filtered_rows.append(kept_row)

        line_rows = filtered_rows
        if not line_rows:
            return list(floating_boxes)

        # Tag each row as either dual-column anchor or single-segment row
        is_dual_anchor = [find_split_x(row) is not None for row in line_rows]

        # Group contiguous rows into bands ("parallel" vs "single")
        bands: list[tuple[str, float | None, list[list[LogicalLine]]]] = []
        n = len(line_rows)
        i = 0
        while i < n:
            if not is_dual_anchor[i]:
                if bands and bands[-1][0] == "single":
                    bands[-1][2].append(line_rows[i])
                else:
                    bands.append(("single", None, [line_rows[i]]))
                i += 1
                continue

            # Start a parallel 2-column band centered around split_x
            anchor_splits: list[float] = []
            j = i
            band_rows: list[list[LogicalLine]] = []
            cur_split = find_split_x(line_rows[i])
            assert cur_split is not None
            anchor_splits.append(cur_split)
            band_rows.append(line_rows[i])

            # Absorb an immediately preceding single-sided header/title segment (e.g., "KT. ...")
            content_center = 0.5 * (content_bounds[0] + content_bounds[2])
            if bands and bands[-1][0] == "single" and bands[-1][2]:
                lead_row = bands[-1][2][-1]
                if len(lead_row) == 1 and not _DOC_TITLE_RE.match(lead_row[0].text):
                    seg = lead_row[0]
                    v_gap_lead = min(l.bbox[1] for l in line_rows[i]) - seg.bbox[3]
                    seg_center = 0.5 * (seg.bbox[0] + seg.bbox[2])
                    crosses_split = seg.bbox[0] < (cur_split - 10.0) and seg.bbox[2] > (cur_split + 10.0)
                    is_page_centered = (
                        abs(seg_center - 0.5 * page_width_pt) <= 22.0
                        or abs(seg_center - content_center) <= 18.0
                    )
                    on_left = seg.bbox[2] <= cur_split + 10.0
                    on_right = seg.bbox[0] >= cur_split - 10.0
                    is_body_wrap = (
                        not seg.is_bold
                        and seg.bbox[2] >= content_bounds[2] - 0.18 * (content_bounds[2] - content_bounds[0])
                        and (seg.bbox[0] - content_bounds[0]) <= 36.0
                    )
                    if (
                        0.0 <= v_gap_lead <= 3.5 * median_h
                        and not crosses_split
                        and not is_page_centered
                        and not is_body_wrap
                        and (on_left or on_right)
                    ):
                        band_rows.insert(0, bands[-1][2].pop())
                        if not bands[-1][2]:
                            bands.pop()

            j += 1

            while j < n:
                nxt_row = line_rows[j]
                if any(_DOC_TITLE_RE.match(s.text) for s in nxt_row):
                    break

                prev_y1 = max(l.bbox[3] for l in band_rows[-1])
                nxt_y0 = min(l.bbox[1] for l in nxt_row)
                v_gap = nxt_y0 - prev_y1

                nxt_split = find_split_x(nxt_row)
                if nxt_split is not None and abs(nxt_split - statistics.median(anchor_splits)) <= 0.18 * page_width_pt:
                    if v_gap <= 4.5 * median_h:
                        anchor_splits.append(nxt_split)
                        band_rows.append(nxt_row)
                        j += 1
                        continue
                    break

                # Single line immediately following a parallel block: absorb if it lies cleanly on one side
                split_ref = statistics.median(anchor_splits)
                if len(nxt_row) == 1:
                    seg = nxt_row[0]
                    has_sig_role = any(
                        _SIGNATURE_ROLE_RE.match(VietnameseAdminPostProcessor.clean_line(l.text))
                        for br in band_rows
                        for l in br
                    )
                    page_center = 0.5 * page_width_pt
                    seg_center = 0.5 * (seg.bbox[0] + seg.bbox[2])
                    # A wide or page-centered line crossing split_ref ends the parallel block immediately
                    crosses_split = seg.bbox[0] < (split_ref - 10.0) and seg.bbox[2] > (split_ref + 10.0)
                    is_page_centered = (
                        abs(seg_center - page_center) <= 22.0
                        or abs(seg_center - content_center) <= 18.0
                    )
                    on_left = seg.bbox[2] <= split_ref + 10.0
                    on_right = seg.bbox[0] >= split_ref - 10.0
                    max_v_gap = 14.0 * median_h if (has_sig_role and on_right) else 5.0 * median_h
                    # Avoid swallowing normal body paragraphs starting on the left after an admin header
                    is_body_wrap = (
                        not seg.is_bold
                        and seg.bbox[2] >= content_bounds[2] - 0.18 * (content_bounds[2] - content_bounds[0])
                        and (seg.bbox[0] - content_bounds[0]) <= 36.0
                    )
                    if (
                        v_gap <= max_v_gap
                        and not crosses_split
                        and not is_page_centered
                        and not is_body_wrap
                        and (on_left or on_right)
                    ):
                        band_rows.append(nxt_row)
                        j += 1
                        continue
                break

            final_split = statistics.median(anchor_splits)
            bands.append(("parallel", final_split, band_rows))
            i = j

        # Convert each band into body elements
        elements: list[Union[SynthesizedParagraph, ParallelColumnsBlock, FloatingTextBox]] = []
        last_y1: float | None = None

        for kind, split_x, band_rows in bands:
            if kind == "single":
                flat_lines = [seg for row in band_rows for seg in row]
                paras = self._synthesize_paragraphs_from_lines(
                    flat_lines,
                    block_bounds=content_bounds,
                    body_pt=body_pt,
                    prev_bottom_y=last_y1,
                    allow_center_sibling_propagation=False,
                )
                if paras:
                    last_y1 = paras[-1].bbox[3]
                    elements.extend(paras)
            else:
                assert split_x is not None
                left_lines: list[LogicalLine] = []
                right_lines: list[LogicalLine] = []
                for row in band_rows:
                    for seg in row:
                        seg_cx = 0.5 * (seg.bbox[0] + seg.bbox[2])
                        if seg_cx <= split_x:
                            left_lines.append(seg)
                        else:
                            right_lines.append(seg)

                c_left, _, c_right, _ = content_bounds
                lx0 = min((l.bbox[0] for l in left_lines), default=c_left)
                lx1 = max((l.bbox[2] for l in left_lines), default=split_x - 10.0)
                rx0 = min((l.bbox[0] for l in right_lines), default=split_x + 10.0)
                rx1 = max((l.bbox[2] for l in right_lines), default=c_right)

                all_band = left_lines + right_lines
                bx0 = min(l.bbox[0] for l in all_band)
                by0 = min(l.bbox[1] for l in all_band)
                bx1 = max(l.bbox[2] for l in all_band)
                by1 = max(l.bbox[3] for l in all_band)

                left_bounds = (lx0, by0, max(lx1, lx0 + 20.0), by1)
                right_bounds = (rx0, by0, max(rx1, rx0 + 20.0), by1)

                left_paras = self._synthesize_paragraphs_from_lines(
                    left_lines,
                    block_bounds=left_bounds,
                    body_pt=body_pt,
                    prev_bottom_y=None,
                    allow_center_sibling_propagation=True,
                )
                right_paras = self._synthesize_paragraphs_from_lines(
                    right_lines,
                    block_bounds=right_bounds,
                    body_pt=body_pt,
                    prev_bottom_y=None,
                    allow_center_sibling_propagation=True,
                )

                left_w = max(80.0, split_x - content_bounds[0])
                right_w = max(80.0, content_bounds[2] - split_x)
                gap_w = max(8.0, rx0 - lx1)

                blk = ParallelColumnsBlock(
                    bbox=(bx0, by0, bx1, by1),
                    left_paragraphs=left_paras,
                    right_paragraphs=right_paras,
                    left_width_pt=left_w,
                    right_width_pt=right_w,
                    gap_width_pt=gap_w,
                )
                self._style_parallel_block(blk)
                elements.append(blk)
                last_y1 = by1

        self._style_body_paragraphs(elements)
        if floating_boxes:
            elements.extend(floating_boxes)
            elements.sort(key=lambda el: (el.bbox[1], el.bbox[0]))

        return elements

    def _extract_signature_seals_and_filter_spans(
        self,
        free_spans: list[WordSpan],
        image_bgr: np.ndarray,
        width_pt: float,
        height_pt: float,
        table_bboxes_pt: Sequence[tuple[float, float, float, float]],
    ) -> tuple[list[WordSpan], list[tuple[tuple[float, float, float, float], tuple[float, float, float, float], FigureBlock]]]:
        """Suppress OCR noise inside red/grayscale stamps and extract clean signature seal crops."""
        if not free_spans or image_bgr is None or image_bgr.size == 0:
            return free_spans, []

        img_h, img_w = image_bgr.shape[:2]
        scale = 72.0 / float(self.dpi)

        # Locate right-column role headers (collapsing stacked "KT. ..." + "PHÓ ..." onto lower line)
        right_spans = sorted(
            [sp for sp in free_spans if _bbox_center(sp.bbox)[0] > 0.44 * width_pt],
            key=lambda s: (0.5 * (s.bbox[1] + s.bbox[3]), s.bbox[0]),
        )
        role_spans: list[WordSpan] = []
        for sp in right_spans:
            cleaned = VietnameseAdminPostProcessor.clean_line(sp.text)
            if len(cleaned) <= 55 and _SIGNATURE_ROLE_RE.match(cleaned):
                role_spans.append(sp)

        collapsed_roles: list[WordSpan] = []
        protected_ids: set[int] = {id(sp) for sp in role_spans}
        for sp in role_spans:
            if collapsed_roles and (sp.bbox[1] - collapsed_roles[-1].bbox[3]) <= 26.0:
                collapsed_roles[-1] = sp
            else:
                collapsed_roles.append(sp)

        # Pair each role header with nearest signer name span below it
        sig_pairs: list[tuple[WordSpan, WordSpan]] = []
        for role_bot in collapsed_roles:
            best_signer: WordSpan | None = None
            for cand in right_spans:
                if cand is role_bot or id(cand) in protected_ids:
                    continue
                v_gap = cand.bbox[1] - role_bot.bbox[3]
                if not (24.0 <= v_gap <= 190.0):
                    continue
                cand_clean = VietnameseAdminPostProcessor.clean_line(cand.text)
                if _is_signer_name_span(cand_clean):
                    best_signer = cand
                    break
            if best_signer is not None:
                protected_ids.add(id(best_signer))
                sig_pairs.append((role_bot, best_signer))

        # Detect large circular HSV red seal components to suppress stray interior OCR fragments
        red_seal_bboxes_pt: list[tuple[float, float, float, float]] = []
        hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
        b_ch, g_ch, r_ch = cv2.split(image_bgr)
        r_dom = r_ch.astype(np.int16) - np.maximum(b_ch, g_ch).astype(np.int16)
        red_mask_full = (
            ((hsv[:, :, 0] <= 16) | (hsv[:, :, 0] >= 160))
            & (hsv[:, :, 1] >= 35)
            & (hsv[:, :, 2] >= 45)
            & (r_dom >= 16)
        )
        if np.any(red_mask_full):
            k_sz = max(9, int(round(0.007 * img_w)) | 1)
            dil = cv2.dilate((red_mask_full.astype(np.uint8) * 255), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_sz, k_sz)))
            n_lbl, _lbls, stats, _ = cv2.connectedComponentsWithStats(dil)
            min_dim = max(55, int(round(0.065 * img_w)))
            for idx in range(1, n_lbl):
                rx, ry, rw, rh, _ = stats[idx]
                aspect = max(rw / max(1, rh), rh / max(1, rw))
                if rw >= min_dim and rh >= min_dim and aspect <= 1.65:
                    red_seal_bboxes_pt.append((rx * scale, ry * scale, (rx + rw) * scale, (ry + rh) * scale))

        # Filter out stamp/signature noise fragments inside signature gaps and circular red seals
        kept_spans: list[WordSpan] = []
        for sp in free_spans:
            if id(sp) in protected_ids:
                kept_spans.append(sp)
                continue
            cleaned = VietnameseAdminPostProcessor.clean_line(sp.text)
            cx, cy = _bbox_center(sp.bbox)
            drop = False

            for role_bot, signer in sig_pairs:
                sig_x0_pt = max(0.42 * width_pt, min(role_bot.bbox[0], signer.bbox[0]) - 95.0)
                if cx >= sig_x0_pt and (role_bot.bbox[3] - 3.0) <= cy <= (signer.bbox[1] + 3.0):
                    words = cleaned.split()
                    if not cleaned.startswith("(") and (
                        len(cleaned) <= 8
                        or bool(re.match(r"^[0-9\W_]+$", cleaned))
                        or (cleaned.isupper() and len(words) <= 2)
                    ):
                        drop = True
                        break

            if not drop and red_seal_bboxes_pt:
                is_admin_tag = bool(_VIET_LIST_OR_CLAUSE_RE.match(cleaned)) or bool(
                    re.match(r"^(?:MẬT|KHẨN|BẢN\s+SỐ)\b", cleaned, re.IGNORECASE)
                )
                if not is_admin_tag and len(cleaned) <= 8:
                    for rx0, ry0, rx1, ry1 in red_seal_bboxes_pt:
                        if rx0 <= cx <= rx1 and ry0 <= cy <= ry1:
                            drop = True
                            break

            if not drop:
                kept_spans.append(sp)

        if not sig_pairs:
            return kept_spans, []

        # Paint kept text spans and bordered tables white so printed role/signer text never duplicates inside the seal crop
        seal_clean = image_bgr.copy()
        for sp in kept_spans:
            sx0, sy0, sx1, sy1 = sp.bbox
            px0 = max(0, int(round(sx0 / scale)) - 2)
            py0 = max(0, int(round(sy0 / scale)) - 2)
            px1 = min(img_w, int(round(sx1 / scale)) + 2)
            py1 = min(img_h, int(round(sy1 / scale)) + 2)
            seal_clean[py0:py1, px0:px1] = (255, 255, 255)
        for tx0, ty0, tx1, ty1 in table_bboxes_pt:
            px0 = max(0, int(round(tx0 / scale)) - 4)
            py0 = max(0, int(round(ty0 / scale)) - 4)
            px1 = min(img_w, int(round(tx1 / scale)) + 4)
            py1 = min(img_h, int(round(ty1 / scale)) + 4)
            seal_clean[py0:py1, px0:px1] = (255, 255, 255)

        extracted: list[tuple[tuple[float, float, float, float], tuple[float, float, float, float], FigureBlock]] = []
        for role_bot, signer in sig_pairs:
            sig_x0_pt = max(0.42 * width_pt, min(role_bot.bbox[0], signer.bbox[0]) - 95.0)
            cx0 = max(0, int(round(sig_x0_pt / scale)))
            cx1 = min(img_w, int(round((width_pt - 8.0) / scale)))
            cy0 = max(0, int(round((role_bot.bbox[3] + 1.5) / scale)))
            cy1 = min(img_h, int(round((signer.bbox[1] - 1.5) / scale)))
            if cx1 - cx0 < 36 or cy1 - cy0 < 24:
                continue

            sub = seal_clean[cy0:cy1, cx0:cx1]
            sub_gray = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
            sub_red = red_mask_full[cy0:cy1, cx0:cx1]
            ink_mask = (sub_gray < 228) | sub_red
            ys, xs = np.where(ink_mask)
            if len(xs) < 250:
                continue

            pad = 4
            tx0 = max(cx0, cx0 + int(xs.min()) - pad)
            ty0 = max(cy0, cy0 + int(ys.min()) - pad)
            tx1 = min(cx1, cx0 + int(xs.max()) + 1 + pad)
            ty1 = min(cy1, cy0 + int(ys.max()) + 1 + pad)
            if tx1 - tx0 < 36 or ty1 - ty0 < 24:
                continue

            crop = seal_clean[ty0:ty1, tx0:tx1]
            ok, encoded = cv2.imencode(".png", crop)
            if not ok:
                continue
            fig = FigureBlock(
                bbox=(tx0 * scale, ty0 * scale, tx1 * scale, ty1 * scale),
                image_png_bytes=encoded.tobytes(),
                width_pt=(tx1 - tx0) * scale,
                height_pt=(ty1 - ty0) * scale,
            )
            extracted.append((role_bot.bbox, signer.bbox, fig))

        return kept_spans, extracted

    def analyze_page(
        self,
        page_index: int,
        width_pt: float,
        height_pt: float,
        image_bgr: np.ndarray,
        detections: Sequence[tuple[tuple[float, float, float, float], str]],
        fitz_page: fitz.Page | None = None,
        extra_table_cell_texts: dict[tuple[int, int, int], str] | None = None,
        precomputed_tables: list[dict] | None = None,
    ) -> ADRTPageDocument:
        """Run Layers 1-5 on a single PDF page and produce a complete `ADRTPageDocument`."""
        # Layer 2a: Bordered table grids & merged cells
        raw_tables = (
            precomputed_tables
            if precomputed_tables is not None
            else self.detect_bordered_tables(image_bgr, fitz_page=fitz_page)
        )
        table_bboxes_pt = [t["bbox"] for t in raw_tables]

        # Build typography-calibrated WordSpans
        all_spans, body_pt = self._build_word_spans(detections, image_bgr)

        # Partition WordSpans into bordered table cells vs free page flow
        free_spans: list[WordSpan] = []
        table_cell_spans: dict[tuple[int, int, int], list[WordSpan]] = {}

        for sp in all_spans:
            cx, cy = _bbox_center(sp.bbox)
            assigned = False
            for t_idx, tbl in enumerate(raw_tables):
                tx0, ty0, tx1, ty1 = tbl["bbox"]
                if (tx0 - 4.0) <= cx <= (tx1 + 4.0) and (ty0 - 4.0) <= cy <= (ty1 + 4.0):
                    for cell in tbl["cells"]:
                        bx0, by0, bx1, by1 = cell["bbox"]
                        if (bx0 - 3.0) <= cx <= (bx1 + 3.0) and (by0 - 3.0) <= cy <= (by1 + 3.0):
                            key = (t_idx, cell["row"], cell["col"])
                            table_cell_spans.setdefault(key, []).append(sp)
                            assigned = True
                            break
                if assigned:
                    break
            if not assigned:
                free_spans.append(sp)

        # Inject any fallback VietOCR cell crops that global detection missed
        if extra_table_cell_texts:
            for (t_idx, r_idx, c_idx), txt in extra_table_cell_texts.items():
                clean = normalize_vietnamese_nfc(txt, collapse_spaces=True).strip()
                if not clean:
                    continue
                key = (t_idx, r_idx, c_idx)
                if not table_cell_spans.get(key) and t_idx < len(raw_tables):
                    match_cell = next(
                        (cl for cl in raw_tables[t_idx]["cells"] if cl["row"] == r_idx and cl["col"] == c_idx),
                        None,
                    )
                    if match_cell is not None:
                        bx0, by0, bx1, by1 = match_cell["bbox"]
                        table_cell_spans[key] = [
                            WordSpan(
                                bbox=(bx0 + 3.0, by0 + 3.0, bx1 - 3.0, by1 - 3.0),
                                text=clean,
                                font_size_pt=max(9.5, body_pt - 0.5),
                                is_bold=(r_idx == 0),
                            )
                        ]

        # Synthesize paragraphs & alignment inside each table cell
        reconstructed_tables: list[ReconstructedTable] = []
        for t_idx, tbl in enumerate(raw_tables):
            cell_objs: list[TableCellBox] = []
            for cell in tbl["cells"]:
                key = (t_idx, cell["row"], cell["col"])
                c_spans = table_cell_spans.get(key, [])
                cell_box = cell["bbox"]
                if c_spans:
                    c_rows = self.group_logical_lines(c_spans, page_width_pt=width_pt)
                    c_lines = [seg for row in c_rows for seg in row]
                    c_paras = self._synthesize_paragraphs_from_lines(
                        c_lines,
                        block_bounds=cell_box,
                        body_pt=body_pt,
                        allow_center_sibling_propagation=True,
                    )
                    if cell["row"] == 0:
                        for p in c_paras:
                            p.is_bold = True
                    cell_text = "\n".join(p.text for p in c_paras)
                else:
                    c_paras = []
                    cell_text = ""

                cell_objs.append(
                    TableCellBox(
                        row=cell["row"],
                        col=cell["col"],
                        row_span=cell["row_span"],
                        col_span=cell["col_span"],
                        bbox=cell_box,
                        paragraphs=c_paras,
                        text=cell_text,
                    )
                )

            reconstructed_tables.append(
                ReconstructedTable(
                    bbox=tbl["bbox"],
                    rows_count=tbl["rows_count"],
                    cols_count=tbl["cols_count"],
                    col_widths_pt=tbl["col_widths_pt"],
                    row_heights_pt=tbl["row_heights_pt"],
                    cells=cell_objs,
                    has_borders=True,
                )
            )

        # Layer 2b: Suppress seal/stamp OCR noise and extract clean signature seal crops before generic figure detection
        free_spans, signature_seals = self._extract_signature_seals_and_filter_spans(
            free_spans=free_spans,
            image_bgr=image_bgr,
            width_pt=width_pt,
            height_pt=height_pt,
            table_bboxes_pt=table_bboxes_pt,
        )

        # Layer 2c: Extract non-text embedded figures outside text, tables & extracted signature seals
        seal_excl_bboxes = [
            (sf.bbox[0] - 8.0, sf.bbox[1] - 8.0, sf.bbox[2] + 8.0, sf.bbox[3] + 8.0)
            for _, _, sf in signature_seals
        ]
        figures = self.extract_figures(
            image_bgr=image_bgr,
            page_w_pt=width_pt,
            page_h_pt=height_pt,
            table_bboxes_pt=table_bboxes_pt,
            ocr_bboxes_pt=[sp.bbox for sp in free_spans] + seal_excl_bboxes,
            fitz_page=fitz_page,
        )

        # Layer 6a: Separate explicit running page Headers (top 4.5%) & Footers (bottom 6.5%) cleanly
        header_spans: list[WordSpan] = []
        footer_spans: list[WordSpan] = []
        body_spans: list[WordSpan] = []

        for sp in free_spans:
            _cx, cy = _bbox_center(sp.bbox)
            if cy <= 0.045 * height_pt and bool(_PAGE_NUMBER_OR_FOOTER_RE.match(sp.text)):
                header_spans.append(sp)
            elif cy >= 0.935 * height_pt and bool(_PAGE_NUMBER_OR_FOOTER_RE.match(sp.text)):
                footer_spans.append(sp)
            else:
                body_spans.append(sp)

        # Layer 1: Calibrate Page Geometry & Margin Envelope from body spans + tables + figures
        env_boxes = [sp.bbox for sp in body_spans] + [t.bbox for t in reconstructed_tables] + [f.bbox for f in figures]
        if env_boxes:
            c_left = min(b[0] for b in env_boxes)
            c_top = min(b[1] for b in env_boxes)
            c_right = max(b[2] for b in env_boxes)
            c_bottom = max(b[3] for b in env_boxes)
            left_margin = round(_clamp(c_left, 18.0, width_pt * 0.22), 1)
            right_margin = round(_clamp(width_pt - c_right, 18.0, width_pt * 0.22), 1)
            top_margin = round(_clamp(c_top, 18.0, height_pt * 0.16), 1)
            bottom_margin = round(_clamp(height_pt - c_bottom, 18.0, height_pt * 0.16), 1)
        else:
            c_left, c_top, c_right, c_bottom = 54.0, 54.0, max(120.0, width_pt - 54.0), max(120.0, height_pt - 54.0)
            left_margin = right_margin = top_margin = bottom_margin = 54.0

        geometry = PageGeometry(
            width_pt=width_pt,
            height_pt=height_pt,
            is_landscape=(width_pt > height_pt),
            top_margin_pt=top_margin,
            bottom_margin_pt=bottom_margin,
            left_margin_pt=left_margin,
            right_margin_pt=right_margin,
            content_left_pt=left_margin,
            content_right_pt=width_pt - right_margin,
            content_top_pt=top_margin,
            content_bottom_pt=height_pt - bottom_margin,
        )
        content_bounds = (
            geometry.content_left_pt,
            geometry.content_top_pt,
            geometry.content_right_pt,
            geometry.content_bottom_pt,
        )

        # Layer 3-5: Header & Footer Paragraphs
        header_rows = self.group_logical_lines(header_spans, page_width_pt=width_pt)
        header_paras = self._synthesize_paragraphs_from_lines(
            [seg for row in header_rows for seg in row],
            block_bounds=content_bounds,
            body_pt=body_pt,
        )
        footer_rows = self.group_logical_lines(footer_spans, page_width_pt=width_pt)
        footer_paras = self._synthesize_paragraphs_from_lines(
            [seg for row in footer_rows for seg in row],
            block_bounds=content_bounds,
            body_pt=body_pt,
        )

        # Layer 3-5: Body flow partitioned into Vertical Zones interleaved with Tables & Figures!
        # Splitting body spans into Y-bands separated by tables avoids falsely merging paragraphs across tables.
        table_intervals = sorted((t.bbox[1], t.bbox[3]) for t in reconstructed_tables)
        zone_spans: list[list[WordSpan]] = []
        sorted_body = sorted(body_spans, key=lambda s: (s.bbox[1], s.bbox[0]))

        def _zone_index_for_y(yc: float) -> int:
            idx = 0
            for ty0, ty1 in table_intervals:
                if yc > 0.5 * (ty0 + ty1):
                    idx += 1
            return idx

        zones_map: dict[int, list[WordSpan]] = {}
        for sp in sorted_body:
            z_idx = _zone_index_for_y(0.5 * (sp.bbox[1] + sp.bbox[3]))
            zones_map.setdefault(z_idx, []).append(sp)

        flow_items: list[tuple[float, BodyElement]] = []
        parallel_blocks: list[ParallelColumnsBlock] = []
        for _z_idx, z_spans in sorted(zones_map.items()):
            z_rows = self.group_logical_lines(z_spans, page_width_pt=width_pt)
            z_elements = self._partition_body_bands(
                z_rows,
                content_bounds=content_bounds,
                page_width_pt=width_pt,
                body_pt=body_pt,
            )
            for el in z_elements:
                if isinstance(el, ParallelColumnsBlock):
                    parallel_blocks.append(el)
                flow_items.append((el.bbox[1], el))

        # Attach clean signature seals directly into matching 2-column signature blocks
        attached_seals: list[FigureBlock] = []
        for role_bbox, signer_bbox, seal_fig in signature_seals:
            matched_block: ParallelColumnsBlock | None = None
            role_yc = 0.5 * (role_bbox[1] + role_bbox[3])
            for blk in parallel_blocks:
                if (blk.bbox[1] - 12.0) <= role_yc <= (blk.bbox[3] + 12.0) and blk.right_paragraphs:
                    matched_block = blk
                    break

            if matched_block is not None and matched_block.right_figure is None:
                after_idx = len(matched_block.right_paragraphs) - 1
                for p_idx, rp in enumerate(matched_block.right_paragraphs):
                    if _vertical_overlap_ratio(rp.bbox, role_bbox) >= 0.35 or _SIGNATURE_ROLE_RE.match(rp.text.strip()):
                        after_idx = p_idx
                    if _vertical_overlap_ratio(rp.bbox, signer_bbox) >= 0.35 or _is_signer_name_span(rp.text):
                        if p_idx > 0:
                            after_idx = min(after_idx, p_idx - 1)
                        break
                matched_block.right_figure = seal_fig
                matched_block.right_figure_after_idx = after_idx
                attached_seals.append(seal_fig)
            else:
                flow_items.append((seal_fig.bbox[1], seal_fig))
                attached_seals.append(seal_fig)

        for tbl in reconstructed_tables:
            flow_items.append((tbl.bbox[1], tbl))
        for fig in figures:
            flow_items.append((fig.bbox[1], fig))

        all_figures = list(figures) + attached_seals
        all_figures.sort(key=lambda f: (f.bbox[1], f.bbox[0]))

        flow_items.sort(key=lambda pair: (pair[0], pair[1].bbox[0]))
        body_elements = [el for _, el in flow_items]

        return ADRTPageDocument(
            page_index=page_index,
            geometry=geometry,
            header_paragraphs=header_paras,
            footer_paragraphs=footer_paras,
            body_elements=body_elements,
            tables=reconstructed_tables,
            figures=all_figures,
        )
