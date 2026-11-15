"""PaddleOCR text detection + VietOCR Vietnamese text recognition engine.

Architecture:
1. `PaddleTextDetector`: Runs PaddleOCR (`det=True, rec=False`) to locate 4-point
   text-line polygons on rasterized PDF pages (>= 200-300 DPI).
2. `perspective_crop_polygon`: Rectifies each 4-point polygon via perspective
   transform (`cv2.getPerspectiveTransform` + `cv2.warpPerspective`) so tilted
   or skewed lines become crisp horizontal crops.
3. `sort_polygons_reading_order`: Groups polygons into visual lines by vertical
   overlap / center-Y tolerance and sorts left-to-right within each line.
4. `VietOCRRecognizer`: Recognizes Vietnamese text using VietOCR (`vgg_transformer`
   or `vgg_seq2seq`) with `pretrained=False`, automatic CUDA/CPU selection, batch
   inference, and canonical Unicode NFC normalization.
"""

from __future__ import annotations

import math
import os
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

import cv2
import numpy as np
import PIL.Image
from PIL import Image

from .vietnamese import normalize_vietnamese_nfc

# Pillow 10+ compatibility shim required by vietocr
if not hasattr(PIL.Image, "ANTIALIAS"):
    PIL.Image.ANTIALIAS = PIL.Image.Resampling.LANCZOS  # type: ignore[attr-defined]


@dataclass(frozen=True)
class DetectedTextRegion:
    """Single detected text region with ordered 4-point polygon and AABB."""

    polygon: np.ndarray  # shape (4, 2), float32: [tl, tr, br, bl]
    bbox: tuple[float, float, float, float]  # (x0, y0, x1, y1)
    crop: Image.Image


def order_quad_points(pts: Sequence[Sequence[float]] | np.ndarray) -> np.ndarray:
    """Order 4 polygon vertices into `[top-left, top-right, bottom-right, bottom-left]`."""
    arr = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    rect = np.zeros((4, 2), dtype=np.float32)

    s = arr.sum(axis=1)
    rect[0] = arr[int(np.argmin(s))]  # top-left
    rect[2] = arr[int(np.argmax(s))]  # bottom-right

    diff = np.diff(arr, axis=1)
    rect[1] = arr[int(np.argmin(diff))]  # top-right
    rect[3] = arr[int(np.argmax(diff))]  # bottom-left
    return rect


def polygon_to_bbox(polygon: np.ndarray) -> tuple[float, float, float, float]:
    """Convert a `(4, 2)` polygon into an axis-aligned `(x0, y0, x1, y1)` tuple."""
    xs = polygon[:, 0]
    ys = polygon[:, 1]
    return (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))


def polygon_local_height(polygon: np.ndarray) -> float:
    """Estimate perpendicular text height from left/right polygon edges."""
    tl, tr, br, bl = order_quad_points(polygon)
    h_left = float(np.linalg.norm(bl - tl))
    h_right = float(np.linalg.norm(br - tr))
    return max(1.0, 0.5 * (h_left + h_right))


def _find_bundled_models_dir() -> Path | None:
    """Locate offline bundled OCR weights folder (`models/`) if packaged alongside app/repo."""
    candidates: list[Path] = []
    env_dir = os.environ.get("PDF2WORD_MODELS_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    exe_dir = Path(sys.executable).resolve().parent
    candidates.append(exe_dir / "models")
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "models")
    candidates.append(Path(__file__).resolve().parent.parent / "models")
    for cand in candidates:
        if cand.is_dir():
            return cand
    return None


def _configure_cpu_runtime() -> int:
    """Tune PyTorch CPU intra-op threads so CPU inference utilizes available cores."""
    import torch

    target_threads = max(4, min(16, os.cpu_count() or 4))
    if not torch.cuda.is_available():
        try:
            if torch.get_num_threads() < target_threads:
                torch.set_num_threads(target_threads)
        except Exception:
            pass
    return target_threads


def deskew_image_bgr(
    image_bgr: np.ndarray,
    angle_range: float = 3.0,
    min_angle: float = 0.2,
    max_angle: float = 5.0,
) -> tuple[np.ndarray, float]:
    """Deskew a slightly tilted page image using horizontal ink-projection variance."""
    if image_bgr is None or image_bgr.size == 0:
        return image_bgr, 0.0

    h, w = image_bgr.shape[:2]
    if h < 120 or w < 120:
        return image_bgr, 0.0

    # Analyze the central 90% region so dark scan borders do not bias projection variance
    y0, y1 = int(0.05 * h), int(0.95 * h)
    x0, x1 = int(0.05 * w), int(0.95 * w)
    gray = cv2.cvtColor(image_bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)

    scale = min(0.25, 520.0 / max(1, gray.shape[1]))
    small_w = max(80, int(round(gray.shape[1] * scale)))
    small_h = max(80, int(round(gray.shape[0] * scale)))
    small = cv2.resize(gray, (small_w, small_h), interpolation=cv2.INTER_AREA)
    ink = (small < 185).astype(np.float32)
    if float(ink.sum()) < 50.0:
        return image_bgr, 0.0

    center = (small_w * 0.5, small_h * 0.5)

    def _projection_variance(angle_deg: float) -> float:
        if abs(angle_deg) < 1e-4:
            rot = ink
        else:
            mat = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
            rot = cv2.warpAffine(
                ink,
                mat,
                (small_w, small_h),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0.0,
            )
        return float(np.var(rot.sum(axis=1)))

    baseline_var = _projection_variance(0.0)
    best_angle = 0.0
    best_var = baseline_var

    # 2-stage coarse-to-fine grid search: 0.5 deg coarse scan -> 0.1 deg fine refinement
    coarse_steps = max(3, int(round((2.0 * angle_range) / 0.5)) + 1)
    for angle in np.linspace(-angle_range, angle_range, coarse_steps):
        if abs(float(angle)) < 1e-4:
            continue
        var = _projection_variance(float(angle))
        if var > best_var:
            best_var = var
            best_angle = float(angle)

    # Fine search over +-0.5 deg around coarse winner at 0.1 deg resolution
    fine_lo = max(-max_angle, best_angle - 0.5)
    fine_hi = min(max_angle, best_angle + 0.5)
    for angle in np.linspace(fine_lo, fine_hi, 11):
        var = _projection_variance(float(angle))
        if var > best_var:
            best_var = var
            best_angle = float(angle)

    if abs(best_angle) < min_angle or abs(best_angle) > max_angle:
        return image_bgr, 0.0
    if baseline_var <= 0.0 or best_var < baseline_var * 1.015:
        return image_bgr, 0.0

    rot_mat = cv2.getRotationMatrix2D((w * 0.5, h * 0.5), best_angle, 1.0)
    deskewed = cv2.warpAffine(
        image_bgr,
        rot_mat,
        (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )
    return deskewed, best_angle


def remove_red_pixels_for_ocr(image_bgr: np.ndarray) -> np.ndarray:
    """Return an OCR copy with large circular/oval red seals whitened while keeping red labels and black text."""
    if image_bgr is None or image_bgr.size == 0:
        return image_bgr

    b_ch, g_ch, r_ch = cv2.split(image_bgr)
    # Require positive R channel dominance so neutral dark-black printed letters under a seal survive;
    # exit before HSV/morphology when a page has no meaningful red ink.
    r_dom = np.subtract(r_ch, np.maximum(b_ch, g_ch), dtype=np.int16)
    if int(np.count_nonzero((r_dom >= 16) & (r_ch >= 80))) < 100:
        return image_bgr

    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    h_ch, s_ch, v_ch = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]

    red_hue = (h_ch <= 16) | (h_ch >= 160)
    red_mask = red_hue & (s_ch >= 35) & (v_ch >= 45) & (r_dom >= 16)
    if not np.any(red_mask):
        return image_bgr

    img_h, img_w = image_bgr.shape[:2]
    # Group nearby red stamp strokes/rings to distinguish round official seals from red horizontal stamps ("MẬT", "KHẨN")
    dilate_k = max(9, int(round(0.007 * img_w)) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dilate_k, dilate_k))
    dilated = cv2.dilate((red_mask.astype(np.uint8) * 255), kernel)
    num_labels, _labels, stats, _ = cv2.connectedComponentsWithStats(dilated)

    min_seal_dim = max(55, int(round(0.065 * img_w)))
    seal_erase_mask = np.zeros_like(red_mask, dtype=bool)
    for lbl in range(1, num_labels):
        rx, ry, rw, rh, _ = stats[lbl]
        aspect = max(rw / max(1, rh), rh / max(1, rw))
        if rw >= min_seal_dim and rh >= min_seal_dim and aspect <= 1.65:
            pad = 4
            sx0 = max(0, int(rx) - pad)
            sy0 = max(0, int(ry) - pad)
            sx1 = min(img_w, int(rx + rw) + pad)
            sy1 = min(img_h, int(ry + rh) + pad)
            seal_erase_mask[sy0:sy1, sx0:sx1] = True

    final_mask = red_mask & seal_erase_mask
    if not np.any(final_mask):
        return image_bgr

    cleaned = image_bgr.copy()
    cleaned[final_mask] = (255, 255, 255)
    return cleaned


def _trim_warped_vertical_bleed(warped_bgr: np.ndarray) -> np.ndarray:
    """Trim outer blank rows and faint adjacent-line edge tips from a warped text crop."""
    h, w = warped_bgr.shape[:2]
    if h < 16 or w < 8:
        return warped_bgr

    gray = cv2.cvtColor(warped_bgr, cv2.COLOR_BGR2GRAY)
    row_ink = np.sum(gray < 200, axis=1)
    if int(row_ink.max()) == 0:
        return warped_bgr

    # Locate active ink rows with a fixed pixel threshold (independent of line width)
    # and bridge short vertical gaps (e.g., gap above Vietnamese diacritics).
    thresh = 2
    active = row_ink >= thresh
    if not np.any(active):
        return warped_bgr

    runs: list[tuple[int, int]] = []
    start: int | None = None
    gap = 0
    for idx, flag in enumerate(active):
        if flag:
            if start is None:
                start = idx
            gap = 0
        elif start is not None:
            gap += 1
            if gap > 6:
                runs.append((start, idx - gap + 1))
                start = None
                gap = 0
    if start is not None:
        end_idx = len(active) - gap if gap > 0 else len(active)
        runs.append((start, end_idx))

    if not runs:
        return warped_bgr

    if len(runs) == 1:
        if h <= 78:
            return warped_bgr
        y0, y1 = runs[0]
    else:
        # Keep the dominant central text run (closest to center weighted by ink mass)
        center_y = 0.5 * h
        best_run = max(
            runs,
            key=lambda r: float(row_ink[r[0] : r[1]].sum()) / (1.0 + abs(0.5 * (r[0] + r[1]) - center_y)),
        )
        y0, y1 = best_run

    # If a 300-DPI wide line box is vertically bloated because faint descenders/ascenders
    # bridged the 6px gap, split at clean ink-profile valleys in the outer top/bottom fringes.
    span_h = y1 - y0
    if span_h > 78 and w > 2.5 * span_h:
        p75 = float(np.percentile(row_ink[y0:y1], 75))
        valley_thresh = max(3, int(round(0.15 * p75)))
        fringe = int(round(0.32 * span_h))
        top_end = y0 + fringe
        bot_start = y1 - fringe

        top_valleys = np.where(row_ink[y0:top_end] <= valley_thresh)[0]
        if len(top_valleys) > 0:
            cand_y0 = y0 + int(top_valleys[-1]) + 1
            if (y1 - cand_y0) >= 24 and int(row_ink[cand_y0:y1].sum()) > int(0.65 * row_ink[y0:y1].sum()):
                y0 = cand_y0

        bot_valleys = np.where(row_ink[bot_start:y1] <= valley_thresh)[0]
        if len(bot_valleys) > 0:
            cand_y1 = bot_start + int(bot_valleys[0])
            if (cand_y1 - y0) >= 24 and int(row_ink[y0:cand_y1].sum()) > int(0.65 * row_ink[y0:y1].sum()):
                y1 = cand_y1

    y0 = max(0, y0 - 4)
    y1 = min(h, y1 + 4)
    if y1 - y0 < 8:
        return warped_bgr
    return warped_bgr[y0:y1, :]


def perspective_crop_polygon(
    image_bgr: np.ndarray,
    polygon: Sequence[Sequence[float]] | np.ndarray,
    padding_px: int | None = None,
) -> Image.Image:
    """Warp a 4-point text polygon into an ink-trimmed, asymmetrically padded RGB PIL image."""
    rect = order_quad_points(polygon).copy()
    tl, tr, br, bl = rect

    width_top = float(np.linalg.norm(tr - tl))
    width_bot = float(np.linalg.norm(br - bl))
    height_left = float(np.linalg.norm(bl - tl))
    height_right = float(np.linalg.norm(br - tr))

    raw_w = max(1.0, max(width_top, width_bot))
    raw_h = max(1.0, max(height_left, height_right))

    # Expand horizontal detection polygons slightly in image space before warping so
    # tall stacked Vietnamese diacritics (e.g. Ố, ệ, ẩ) sitting right above the DBNet
    # polygon edge are sampled cleanly rather than clipped prior to white padding.
    if raw_w >= raw_h * 0.8:
        vec_x = 0.5 * ((tr - tl) + (br - bl))
        ux = vec_x / max(1.0, float(np.linalg.norm(vec_x)))
        vec_y = 0.5 * ((bl - tl) + (br - tr))
        uy = vec_y / max(1.0, float(np.linalg.norm(vec_y)))

        exp_top = min(9.0, max(4.0, 0.16 * raw_h)) if raw_h <= 68.0 else 0.0
        exp_bot = min(4.0, max(2.0, 0.05 * raw_h)) if raw_h <= 68.0 else 0.0
        exp_lr = min(6.0, max(3.0, 0.08 * raw_h))

        tl = tl - uy * exp_top - ux * exp_lr
        tr = tr - uy * exp_top + ux * exp_lr
        br = br + uy * exp_bot + ux * exp_lr
        bl = bl + uy * exp_bot - ux * exp_lr
        rect = np.array([tl, tr, br, bl], dtype=np.float32)

        width_top = float(np.linalg.norm(tr - tl))
        width_bot = float(np.linalg.norm(br - bl))
        height_left = float(np.linalg.norm(bl - tl))
        height_right = float(np.linalg.norm(br - tr))

    dst_w = max(1, int(round(max(width_top, width_bot))))
    dst_h = max(1, int(round(max(height_left, height_right))))

    dst_pts = np.array(
        [
            [0.0, 0.0],
            [float(dst_w - 1), 0.0],
            [float(dst_w - 1), float(dst_h - 1)],
            [0.0, float(dst_h - 1)],
        ],
        dtype=np.float32,
    )

    # Slice a tight ROI around the quad (4px margin covers 4x4 cubic interpolation footprint)
    # so cv2.warpPerspective runs on the text band instead of the full 300-DPI page buffer.
    img_h, img_w = image_bgr.shape[:2]
    margin = 4
    src_x0 = max(0, int(np.floor(float(rect[:, 0].min()))) - margin)
    src_y0 = max(0, int(np.floor(float(rect[:, 1].min()))) - margin)
    src_x1 = min(img_w, int(np.ceil(float(rect[:, 0].max()))) + margin)
    src_y1 = min(img_h, int(np.ceil(float(rect[:, 1].max()))) + margin)

    if src_x1 > src_x0 and src_y1 > src_y0:
        src_roi = image_bgr[src_y0:src_y1, src_x0:src_x1]
        local_rect = rect - np.array([src_x0, src_y0], dtype=np.float32)
    else:
        src_roi = image_bgr
        local_rect = rect

    matrix = cv2.getPerspectiveTransform(local_rect, dst_pts)
    warped = cv2.warpPerspective(
        src_roi,
        matrix,
        (dst_w, dst_h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )

    # Rotate vertical text strips upright for horizontal sequence recognition
    if warped.shape[0] >= warped.shape[1] * 1.5 and warped.shape[0] > 24:
        warped = cv2.rotate(warped, cv2.ROTATE_90_COUNTERCLOCKWISE)

    warped = _trim_warped_vertical_bleed(warped)
    ih = max(12, warped.shape[0])

    if padding_px is None:
        pad_top = max(6, int(round(0.12 * ih)))
        pad_bot = max(6, int(round(0.10 * ih)))
        pad_left = max(8, int(round(0.12 * ih)))
        pad_right = max(8, int(round(0.12 * ih)))
    else:
        base_pad = max(0, int(padding_px))
        pad_top = pad_bot = pad_left = pad_right = base_pad

    if pad_top or pad_bot or pad_left or pad_right:
        warped = cv2.copyMakeBorder(
            warped,
            pad_top,
            pad_bot,
            pad_left,
            pad_right,
            cv2.BORDER_CONSTANT,
            value=(255, 255, 255),
        )

    rgb = cv2.cvtColor(warped, cv2.COLOR_BGR2RGB)
    return Image.fromarray(rgb)


def sort_polygons_reading_order(polygons: Iterable[np.ndarray]) -> list[np.ndarray]:
    """Sort 4-point polygons into natural reading order (top-to-bottom rows, left-to-right)."""
    ordered = [order_quad_points(p) for p in polygons]
    if len(ordered) <= 1:
        return ordered

    items: list[tuple[np.ndarray, tuple[float, float, float, float], float, float]] = []
    heights: list[float] = []
    for poly in ordered:
        x0, y0, x1, y1 = polygon_to_bbox(poly)
        h = polygon_local_height(poly)
        yc = float(np.mean(poly[:, 1]))
        items.append((poly, (x0, y0, x1, y1), yc, h))
        heights.append(h)

    median_h = statistics.median(heights)
    # Sort initially by center-Y then left-X
    items.sort(key=lambda item: (item[2], item[1][0]))

    rows: list[list[tuple[np.ndarray, tuple[float, float, float, float], float, float]]] = []
    for item in items:
        _, bbox, yc, h = item
        placed = False
        for row in rows:
            # Vertically stacked polygons with substantial X-overlap belong to different rows
            cand_w = max(1.0, bbox[2] - bbox[0])
            x_conflicts = False
            for _, r_bbox, _, _ in row:
                r_w = max(1.0, r_bbox[2] - r_bbox[0])
                x_ov = max(0.0, min(bbox[2], r_bbox[2]) - max(bbox[0], r_bbox[0]))
                if x_ov / min(cand_w, r_w) > 0.25:
                    x_conflicts = True
                    break
            if x_conflicts:
                continue

            row_yc = statistics.mean(r[2] for r in row)
            row_h = max(median_h * 0.7, statistics.mean(r[3] for r in row))
            row_y0 = min(r[1][1] for r in row)
            row_y1 = max(r[1][3] for r in row)
            overlap = max(0.0, min(bbox[3], row_y1) - max(bbox[1], row_y0))
            min_h = min(h, row_h)
            if abs(yc - row_yc) <= 0.45 * min_h or (min_h > 0 and overlap / min_h >= 0.50):
                row.append(item)
                placed = True
                break
        if not placed:
            rows.append([item])

    # Order rows top-to-bottom by mean Y-center, and boxes within each row left-to-right
    rows.sort(key=lambda row: statistics.mean(r[2] for r in row))
    sorted_polys: list[np.ndarray] = []
    for row in rows:
        row.sort(key=lambda r: r[1][0])
        sorted_polys.extend(r[0] for r in row)
    return sorted_polys


def _has_vertical_divider_between(
    image_bgr: np.ndarray,
    x_left: int,
    x_right: int,
    y_top: int,
    y_bot: int,
) -> bool:
    """Detect whether a dark vertical table rule separates two horizontally adjacent boxes."""
    if x_right <= x_left + 1 or y_bot <= y_top + 6:
        return False
    h, w = image_bgr.shape[:2]
    x0 = max(0, x_left)
    x1 = min(w, x_right)
    y0 = max(0, y_top)
    y1 = min(h, y_bot)
    if x1 <= x0 or y1 <= y0:
        return False
    gray = cv2.cvtColor(image_bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    dark = gray < 150
    col_cov = dark.mean(axis=0)
    return bool(float(col_cov.max()) >= 0.78)


def merge_same_row_polygons(
    image_bgr: np.ndarray,
    polygons: Sequence[np.ndarray],
) -> list[np.ndarray]:
    """Merge fragmented horizontal polygons belonging to the same visual line before OCR."""
    ordered = sort_polygons_reading_order(polygons)
    if len(ordered) <= 1:
        return ordered

    heights = [polygon_local_height(p) for p in ordered]
    median_h = statistics.median(heights)

    merged: list[np.ndarray] = [ordered[0]]
    for poly in ordered[1:]:
        prev = merged[-1]
        px0, py0, px1, py1 = polygon_to_bbox(prev)
        cx0, cy0, cx1, cy1 = polygon_to_bbox(poly)
        ph = polygon_local_height(prev)
        ch = polygon_local_height(poly)
        pyc = float(np.mean(prev[:, 1]))
        cyc = float(np.mean(poly[:, 1]))

        min_h = min(ph, ch)
        y_overlap = max(0.0, min(py1, cy1) - max(py0, cy0))
        x_ov = max(0.0, min(px1, cx1) - max(px0, cx0))
        pw = max(1.0, px1 - px0)
        cw = max(1.0, cx1 - cx0)
        gap_x = cx0 - px1

        same_line_y = (
            abs(cyc - pyc) <= 0.35 * min_h
            and (y_overlap / max(1.0, min_h)) >= 0.60
        )
        max_gap = max(0.85 * max(ph, ch, median_h), 28.0)
        if min(pw, cw) <= 2.2 * median_h:
            max_gap = max(max_gap, 1.35 * max(ph, ch, median_h))
        max_gap = min(max_gap, 65.0)
        reasonable_gap = (-0.75 * max(ph, ch) <= gap_x <= max_gap) and (x_ov / min(pw, cw) <= 0.25)

        if same_line_y and reasonable_gap:
            divider = False
            if gap_x > 2.0:
                divider = _has_vertical_divider_between(
                    image_bgr,
                    int(px1) - 1,
                    int(cx0) + 1,
                    int(min(py0, cy0)),
                    int(max(py1, cy1)),
                )
            if not divider:
                ux0 = min(px0, cx0)
                uy0 = min(py0, cy0)
                ux1 = max(px1, cx1)
                uy1 = max(py1, cy1)
                merged[-1] = np.array(
                    [[ux0, uy0], [ux1, uy0], [ux1, uy1], [ux0, uy1]],
                    dtype=np.float32,
                )
                continue

        merged.append(poly)

    return merged


class PaddleTextDetector:
    """Text-region detector backed by PaddleOCR (`det=True, rec=False`)."""

    def __init__(self, use_angle_cls: bool = True, use_gpu: bool | None = None) -> None:
        self.use_angle_cls = use_angle_cls
        self.use_gpu = use_gpu
        self._ocr = None

    def _get_detector(self):
        if self._ocr is None:
            # On Windows, PyTorch must be imported before PaddlePaddle to avoid
            # OpenMP / MKL DLL symbol conflicts in torch/lib/shm.dll.
            import torch
            from paddleocr import PaddleOCR

            cpu_threads = _configure_cpu_runtime()
            use_cpu = self.use_gpu is False or (self.use_gpu is None and not torch.cuda.is_available())
            kwargs = {
                "use_angle_cls": self.use_angle_cls,
                "det": True,
                "rec": False,
                "show_log": False,
                "det_limit_side_len": 2560,
                "det_db_thresh": 0.22,
                "det_db_box_thresh": 0.35,
                "det_db_unclip_ratio": 1.45,
            }
            if self.use_gpu is not None:
                kwargs["use_gpu"] = self.use_gpu
            models_dir = _find_bundled_models_dir()
            if models_dir is not None:
                det_dir = models_dir / "paddleocr" / "det"
                cls_dir = models_dir / "paddleocr" / "cls"
                rec_dir = models_dir / "paddleocr" / "rec"
                if (det_dir / "inference.pdmodel").is_file():
                    kwargs["det_model_dir"] = str(det_dir)
                if (cls_dir / "inference.pdmodel").is_file():
                    kwargs["cls_model_dir"] = str(cls_dir)
                if (rec_dir / "inference.pdmodel").is_file():
                    kwargs["rec_model_dir"] = str(rec_dir)
            if use_cpu:
                kwargs["enable_mkldnn"] = True
                kwargs["cpu_threads"] = cpu_threads
            try:
                self._ocr = PaddleOCR(**kwargs)
            except TypeError:
                kwargs.pop("enable_mkldnn", None)
                kwargs.pop("cpu_threads", None)
                self._ocr = PaddleOCR(**kwargs)
            _configure_cpu_runtime()
        return self._ocr

    def detect(self, image_bgr: np.ndarray, sort_reading_order: bool = True) -> list[np.ndarray]:
        """Return detected 4-point text polygons `(4, 2)` in reading order."""
        if image_bgr is None or image_bgr.size == 0:
            return []

        detector = self._get_detector()
        raw = detector.ocr(image_bgr, det=True, rec=False, cls=False)
        if not raw:
            return []

        # PaddleOCR returns either [[poly1, poly2, ...]] or [poly1, ...] depending on version
        candidates = raw[0] if isinstance(raw[0], list) and raw[0] and isinstance(raw[0][0], (list, np.ndarray)) else raw
        if not candidates:
            return []

        polygons: list[np.ndarray] = []
        for poly in candidates:
            if poly is None:
                continue
            arr = np.asarray(poly, dtype=np.float32)
            if arr.shape == (4, 2):
                polygons.append(order_quad_points(arr))

        # Suppress tiny scan-dust speckle false positives that sit between body text lines
        if polygons:
            img_h = float(image_bgr.shape[0])
            poly_heights = [polygon_local_height(p) for p in polygons]
            median_h = statistics.median(poly_heights) if poly_heights else 0.0
            filtered: list[np.ndarray] = []
            for poly, lh in zip(polygons, poly_heights):
                x0, y0, x1, y1 = polygon_to_bbox(poly)
                bw = x1 - x0
                bh = y1 - y0
                yc = 0.5 * (y0 + y1)
                if min(bw, bh) < 14.0:
                    continue
                if (
                    img_h >= 1200.0
                    and len(polygons) >= 8
                    and median_h > 0.0
                    and (0.05 * img_h) <= yc <= (0.94 * img_h)
                    and lh < 0.40 * median_h
                    and bw < 1.2 * median_h
                ):
                    continue
                filtered.append(poly)
            polygons = filtered

        if sort_reading_order and polygons:
            return sort_polygons_reading_order(polygons)
        return polygons

    def detect_and_crop(
        self,
        image_bgr: np.ndarray,
        merge_fragments: bool = True,
    ) -> list[DetectedTextRegion]:
        """Detect text polygons, merge inline fragments, and return rectified crops in reading order."""
        polygons = self.detect(image_bgr, sort_reading_order=True)
        if merge_fragments and len(polygons) > 1:
            polygons = merge_same_row_polygons(image_bgr, polygons)

        regions: list[DetectedTextRegion] = []
        for poly in polygons:
            bbox = polygon_to_bbox(poly)
            crop = perspective_crop_polygon(image_bgr, poly)
            regions.append(DetectedTextRegion(polygon=poly, bbox=bbox, crop=crop))
        return regions


class VietOCRRecognizer:
    """Vietnamese text recognizer backed by VietOCR (`vgg_transformer` / `vgg_seq2seq`)."""

    SUPPORTED_MODELS = ("vgg_transformer", "vgg_seq2seq")

    def __init__(self, model_name: str | None = None, batch_size: int = 16) -> None:
        chosen = model_name or os.environ.get("PDF2WORD_VIETOCR_MODEL", "vgg_transformer")
        if chosen not in self.SUPPORTED_MODELS:
            raise ValueError(f"Unsupported VietOCR model '{chosen}'. Choose from {self.SUPPORTED_MODELS}.")
        self.model_name = chosen
        self.batch_size = max(1, int(batch_size))
        self._predictor = None
        self._device: str | None = None

    @property
    def device(self) -> str:
        if self._device is None:
            import torch

            self._device = "cuda:0" if torch.cuda.is_available() else "cpu"
        return self._device

    def build_config(self) -> dict:
        """Build VietOCR configuration with `pretrained=False`, offline local config/weights, and auto CUDA/CPU device."""
        from vietocr.tool.config import Cfg

        models_dir = _find_bundled_models_dir()
        local_cfg = (models_dir / "vietocr" / f"{self.model_name}.yml") if models_dir is not None else None
        if local_cfg is not None and local_cfg.is_file():
            config = Cfg.load_config_from_file(str(local_cfg))
        else:
            config = Cfg.load_config_from_name(self.model_name)

        config["cnn"]["pretrained"] = False
        config["device"] = self.device
        if models_dir is not None:
            local_weight = models_dir / "vietocr" / f"{self.model_name}.pth"
            if local_weight.is_file():
                config["weights"] = str(local_weight)
        return config

    def _get_predictor(self):
        if self._predictor is None:
            from vietocr.tool.predictor import Predictor

            _configure_cpu_runtime()
            config = self.build_config()
            self._predictor = Predictor(config)
            if getattr(self._predictor, "model", None) is not None:
                self._predictor.model.eval()
        return self._predictor

    @staticmethod
    def _supports_kv_cache(predictor) -> bool:
        """Return True if predictor wraps an autoregressive LanguageTransformer without beam search."""
        config = getattr(predictor, "config", None)
        model = getattr(predictor, "model", None)
        vocab = getattr(predictor, "vocab", None)
        if not isinstance(config, dict) or model is None or vocab is None:
            return False
        if config.get("predictor", {}).get("beamsearch", False):
            return False
        lt = getattr(model, "transformer", None)
        if lt is None or not hasattr(lt, "transformer") or not hasattr(lt, "embed_tgt") or not hasattr(lt, "pos_enc"):
            return False
        decoder = getattr(lt.transformer, "decoder", None)
        layers = getattr(decoder, "layers", None)
        if not layers:
            return False
        return all(not getattr(layer, "norm_first", False) for layer in layers)

    @staticmethod
    def _scaled_dot_product(q, k, v):
        import torch.nn.functional as F

        if hasattr(F, "scaled_dot_product_attention"):
            return F.scaled_dot_product_attention(q, k, v)
        scores = q @ k.transpose(-2, -1) / math.sqrt(q.size(-1))
        return F.softmax(scores, dim=-1) @ v

    def _translate_transformer_kv_cache(
        self,
        predictor,
        batch_img,
        max_seq_length: int = 128,
        sos_token: int = 1,
        eos_token: int = 2,
    ) -> np.ndarray:
        """Greedy LanguageTransformer decoder with precomputed cross-attention and preallocated self-attention KV cache."""
        import torch
        import torch.nn.functional as F

        model = predictor.model
        model.eval()
        device = batch_img.device
        bsz = int(batch_img.size(0))
        lt = model.transformer
        d_model = int(lt.d_model)
        scale = math.sqrt(d_model)
        layers = lt.transformer.decoder.layers
        nhead = int(layers[0].self_attn.num_heads)
        head_dim = d_model // nhead

        src = model.cnn(batch_img)
        memory = lt.forward_encoder(src)  # (src_len, bsz, d_model)
        src_len = int(memory.size(0))
        mem_t = memory.transpose(0, 1).contiguous()

        # Precompute cross-attention K_mem / V_mem projections once per batch instead of every step
        cross_kv = []
        for layer in layers:
            mha = layer.multihead_attn
            w_q, w_k, w_v = mha.in_proj_weight.chunk(3, dim=0)
            if mha.in_proj_bias is not None:
                b_q, b_k, b_v = mha.in_proj_bias.chunk(3, dim=0)
            else:
                b_q = b_k = b_v = None
            k_m = F.linear(mem_t, w_k, b_k).view(bsz, src_len, nhead, head_dim).transpose(1, 2).contiguous()
            v_m = F.linear(mem_t, w_v, b_v).view(bsz, src_len, nhead, head_dim).transpose(1, 2).contiguous()
            cross_kv.append((w_q, b_q, k_m, v_m, mha.out_proj))

        pe = lt.pos_enc.pe
        max_steps = min(max_seq_length + 1, int(pe.size(0)))
        self_k_buf = [
            torch.empty((bsz, nhead, max_steps, head_dim), dtype=mem_t.dtype, device=device) for _ in layers
        ]
        self_v_buf = [
            torch.empty((bsz, nhead, max_steps, head_dim), dtype=mem_t.dtype, device=device) for _ in layers
        ]
        tokens = torch.empty((bsz, max_steps + 1), dtype=torch.long, device=device)
        tokens[:, 0] = sos_token
        finished = torch.zeros(bsz, dtype=torch.bool, device=device)
        decoder_norm = getattr(lt.transformer.decoder, "norm", None)

        for step in range(max_steps):
            last_tok = tokens[:, step : step + 1]
            x = lt.embed_tgt(last_tok) * scale + pe[step : step + 1].transpose(0, 1)
            cur_len = step + 1

            for idx, layer in enumerate(layers):
                sa = layer.self_attn
                qkv = F.linear(x, sa.in_proj_weight, sa.in_proj_bias)
                q, k, v = qkv.chunk(3, dim=-1)
                q = q.view(bsz, 1, nhead, head_dim).transpose(1, 2)
                self_k_buf[idx][:, :, step:cur_len, :] = k.view(bsz, 1, nhead, head_dim).transpose(1, 2)
                self_v_buf[idx][:, :, step:cur_len, :] = v.view(bsz, 1, nhead, head_dim).transpose(1, 2)

                sa_out = self._scaled_dot_product(
                    q,
                    self_k_buf[idx][:, :, :cur_len, :],
                    self_v_buf[idx][:, :, :cur_len, :],
                )
                sa_out = sa.out_proj(sa_out.transpose(1, 2).contiguous().view(bsz, 1, d_model))
                x = layer.norm1(x + sa_out)

                w_q, b_q, k_m, v_m, out_proj = cross_kv[idx]
                q_c = F.linear(x, w_q, b_q).view(bsz, 1, nhead, head_dim).transpose(1, 2)
                ca_out = self._scaled_dot_product(q_c, k_m, v_m)
                ca_out = out_proj(ca_out.transpose(1, 2).contiguous().view(bsz, 1, d_model))
                x = layer.norm2(x + ca_out)

                ffn = layer.linear2(layer.activation(layer.linear1(x)))
                x = layer.norm3(x + ffn)

            if decoder_norm is not None:
                x = decoder_norm(x)
            logits = lt.fc(x[:, 0, :])
            next_tok = torch.argmax(logits, dim=-1)
            tokens[:, cur_len] = next_tok
            finished |= next_tok == eos_token
            if bool(torch.all(finished)):
                return tokens[:, : cur_len + 1].cpu().numpy()

        return tokens[:, : max_steps + 1].cpu().numpy()

    def recognize(self, crop: Image.Image) -> str:
        """Recognize Vietnamese text from a single horizontally aligned PIL crop."""
        if crop.width < 3 or crop.height < 3:
            return ""
        return self.recognize_batch([crop])[0]

    def recognize_batch(self, crops: Sequence[Image.Image]) -> list[str]:
        """Recognize a sequence of PIL crops using exact-width bucketing, KV-cache decoding, and NFC normalization."""
        if not crops:
            return []
        import torch

        predictor = self._get_predictor()
        _configure_cpu_runtime()
        results: list[str] = [""] * len(crops)
        valid_indices = [i for i, im in enumerate(crops) if im.width >= 3 and im.height >= 3]
        if not valid_indices:
            return results

        if self._supports_kv_cache(predictor):
            from vietocr.tool.translate import process_input

            ds_cfg = predictor.config["dataset"]
            img_h = int(ds_cfg["image_height"])
            min_w = int(ds_cfg["image_min_width"])
            max_w = int(ds_cfg["image_max_width"])

            # Bucket across the entire page by exact resized tensor width so zero padding is added
            buckets: dict[int, list[tuple[int, torch.Tensor]]] = {}
            for orig_idx in valid_indices:
                t_im = process_input(crops[orig_idx].convert("RGB"), img_h, min_w, max_w)
                buckets.setdefault(int(t_im.shape[-1]), []).append((orig_idx, t_im))

            with torch.inference_mode():
                for _width, items in buckets.items():
                    for start in range(0, len(items), self.batch_size):
                        sub = items[start : start + self.batch_size]
                        batch_tensor = torch.cat([t for _, t in sub], dim=0).to(predictor.device)
                        seqs = self._translate_transformer_kv_cache(predictor, batch_tensor)
                        decoded = predictor.vocab.batch_decode(seqs.tolist())
                        for (orig_idx, _), pred in zip(sub, decoded):
                            results[orig_idx] = normalize_vietnamese_nfc(str(pred), collapse_spaces=True)
            return results

        # Fallback path for Seq2Seq / beamsearch / custom mock predictors
        valid_pairs = [(i, crops[i].convert("RGB")) for i in valid_indices]
        valid_pairs.sort(key=lambda pair: pair[1].width / max(1, pair[1].height))

        with torch.inference_mode():
            for start in range(0, len(valid_pairs), self.batch_size):
                chunk = valid_pairs[start : start + self.batch_size]
                chunk_imgs = [im for _, im in chunk]
                if hasattr(predictor, "predict_batch") and len(chunk_imgs) > 1:
                    try:
                        preds = predictor.predict_batch(chunk_imgs)
                    except Exception:
                        preds = [predictor.predict(im) for im in chunk_imgs]
                else:
                    preds = [predictor.predict(im) for im in chunk_imgs]

                for (orig_idx, _), pred in zip(chunk, preds):
                    results[orig_idx] = normalize_vietnamese_nfc(str(pred), collapse_spaces=True)

        return results


class PaddleVietOcrEngine:
    """End-to-end OCR engine pairing PaddleOCR detection with VietOCR recognition."""

    def __init__(self, model_name: str | None = None, batch_size: int = 16) -> None:
        self.detector = PaddleTextDetector(use_angle_cls=True)
        self.recognizer = VietOCRRecognizer(model_name=model_name, batch_size=batch_size)

    def recognize_image(
        self,
        image_bgr: np.ndarray,
        strip_red_seals: bool = True,
        on_stage: Callable[[str, int], None] | None = None,
    ) -> list[tuple[tuple[float, float, float, float], str]]:
        """Detect text polygons via PaddleOCR, perspective-crop, and recognize via VietOCR."""
        ocr_input = remove_red_pixels_for_ocr(image_bgr) if strip_red_seals else image_bgr
        if on_stage is not None:
            on_stage("detect", 0)
        regions = self.detector.detect_and_crop(ocr_input)
        if not regions:
            return []
        if on_stage is not None:
            on_stage("recognize", len(regions))
        texts = self.recognizer.recognize_batch([r.crop for r in regions])
        output: list[tuple[tuple[float, float, float, float], str]] = []
        for region, text in zip(regions, texts):
            if text:
                output.append((region.bbox, text))
        return output
