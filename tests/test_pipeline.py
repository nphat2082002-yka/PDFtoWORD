"""Automated verification suite for PaddleOCR + VietOCR PDF-to-DOCX pipeline."""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pytest
import pymupdf
from PIL import Image, ImageDraw

from pdf2word import (
    ADRTLayoutEngine,
    AbbyyLocalAdapter,
    LogicalLine,
    PaddleTextDetector,
    PageKind,
    VietOCRRecognizer,
    analyze_pdf,
    convert_pdf_to_docx,
    detect_bad_encoding_or_garbled,
    is_vietnamese_nfc,
    normalize_vietnamese_nfc,
    order_quad_points,
    perspective_crop_polygon,
    should_merge_into_paragraph,
    sort_polygons_reading_order,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / "pdf2word"
SAMPLES_DIR = REPO_ROOT / "samples"
ABBYY_DIR = Path(r"C:\Program Files\ABBYY FineReader 16")


def _build_sample_vietnamese_pdf(pdf_path: Path) -> None:
    """Create a multi-element Vietnamese digital PDF (headings, NFD+NFC text, table, image)."""
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)  # A4

    times_regular = r"C:\Windows\Fonts\times.ttf"
    times_bold = r"C:\Windows\Fonts\timesbd.ttf"
    font_reg = times_regular if os.path.exists(times_regular) else None
    font_bld = times_bold if os.path.exists(times_bold) else font_reg

    if font_reg:
        page.insert_font(fontname="F_REG", fontfile=font_reg)
        page.insert_font(fontname="F_BLD", fontfile=font_bld)
        f_reg, f_bld = "F_REG", "F_BLD"
    else:
        f_reg, f_bld = "helv", "hebo"

    # Title & Section Heading
    page.insert_text(
        (50, 65),
        "BÁO CÁO KỸ THUẬT CHUYỂN ĐỔI PDF SANG WORD",
        fontname=f_bld,
        fontsize=18,
    )
    page.insert_text(
        (50, 100),
        "1. Tổng quan hệ thống xử lý Tiếng Việt",
        fontname=f_bld,
        fontsize=14,
    )

    # Paragraph including NFD-decomposed Vietnamese text to test Stage 4 NFC composition
    nfd_para = unicodedata.normalize(
        "NFD",
        "Hệ thống hỗ trợ đầy đủ các nguyên âm tiếng Việt: ă, â, đ, ê, ô, ơ, ư "
        "cùng 5 thanh điệu (huyền, sắc, hỏi, ngã, nặng) như: hòa bình, kỹ thuật, "
        "ngôn ngữ, chuẩn hóa dữ liệu mở rộng.",
    )
    page.insert_textbox(
        pymupdf.Rect(50, 120, 545, 210),
        nfd_para,
        fontname=f_reg,
        fontsize=11.5,
        align=0,
    )

    # Ruled Data Table (3 columns x 4 rows)
    x_cols = [50, 130, 360, 545]
    y_rows = [230, 260, 290, 320, 350]
    shape = page.new_shape()
    for y in y_rows:
        shape.draw_line((x_cols[0], y), (x_cols[-1], y))
    for x in x_cols:
        shape.draw_line((x, y_rows[0]), (x, y_rows[-1]))
    shape.finish(color=(0, 0, 0), width=1.0)
    shape.commit()

    table_data = [
        ["Mã số", "Thành phần xử lý", "Trạng thái"],
        ["01", "Phân tích bố cục & bảng biểu", "Hoạt động tốt"],
        ["02", "Chuẩn hóa Unicode dựng sẵn NFC", "Đã kiểm chứng"],
        ["03", "Xuất định dạng OpenXML (.docx)", "Sẵn sàng"],
    ]
    for r_idx, row in enumerate(table_data):
        fn = f_bld if r_idx == 0 else f_reg
        y_top = y_rows[r_idx] + 8
        for c_idx, cell_text in enumerate(row):
            page.insert_text((x_cols[c_idx] + 6, y_top + 11), cell_text, fontname=fn, fontsize=10.5)

    # Embedded PNG image
    img = Image.new("RGB", (240, 80), color=(240, 246, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([4, 4, 235, 75], outline=(30, 90, 170), width=3)
    draw.ellipse([20, 20, 60, 60], fill=(30, 90, 170))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    page.insert_image(pymupdf.Rect(50, 380, 290, 460), stream=buf.getvalue())

    doc.save(str(pdf_path))
    doc.close()


def _build_scanned_pdf_from_digital(digital_pdf: Path, scanned_pdf: Path) -> None:
    """Rasterize the digital PDF to an image-only PDF to exercise PaddleOCR + VietOCR."""
    src = pymupdf.open(str(digital_pdf))
    dst = pymupdf.open()
    for page in src:
        pix = page.get_pixmap(dpi=220, alpha=False)
        img_page = dst.new_page(width=page.rect.width, height=page.rect.height)
        img_page.insert_image(img_page.rect, stream=pix.tobytes("png"))
    dst.save(str(scanned_pdf))
    src.close()
    dst.close()


def test_no_tesseract_or_rapidocr_in_package():
    forbidden = ("tesseract", "pytesseract", "tessdata", "rapidocr")
    for py_file in PKG_DIR.glob("*.py"):
        content = py_file.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in content, f"Forbidden OCR reference '{token}' found in {py_file.name}"


def test_perspective_crop_and_reading_order_geometry():
    # Create synthetic tilted rectangle on white canvas
    canvas = np.full((200, 400, 3), 255, dtype=np.uint8)
    skewed_poly = np.array([[60, 40], [320, 60], [315, 110], [55, 90]], dtype=np.float32)
    cv2.fillPoly(canvas, [skewed_poly.astype(np.int32)], (220, 235, 250))

    ordered = order_quad_points(skewed_poly[[2, 0, 3, 1]])
    assert np.allclose(ordered[0], [60, 40])
    assert np.allclose(ordered[1], [320, 60])
    assert np.allclose(ordered[2], [315, 110])
    assert np.allclose(ordered[3], [55, 90])

    crop = perspective_crop_polygon(canvas, skewed_poly)
    assert isinstance(crop, Image.Image)
    assert crop.mode == "RGB"
    assert crop.width > crop.height
    assert crop.width >= 250 and crop.height >= 45

    # Reading order: line 1 (right then left), line 2 (middle)
    p_r1_right = np.array([[220, 22], [340, 22], [340, 48], [220, 48]], dtype=np.float32)
    p_r1_left = np.array([[40, 20], [180, 20], [180, 46], [40, 46]], dtype=np.float32)
    p_r2 = np.array([[40, 90], [200, 90], [200, 116], [40, 116]], dtype=np.float32)

    sorted_polys = sort_polygons_reading_order([p_r2, p_r1_right, p_r1_left])
    assert np.allclose(sorted_polys[0], p_r1_left)
    assert np.allclose(sorted_polys[1], p_r1_right)
    assert np.allclose(sorted_polys[2], p_r2)


def test_vietocr_config_and_vietnamese_nfc():
    rec = VietOCRRecognizer(model_name="vgg_transformer")
    cfg = rec.build_config()
    assert cfg["cnn"]["pretrained"] is False
    assert cfg["device"] in ("cpu", "cuda:0")

    decomposed = unicodedata.normalize("NFD", "Tiếng Việt chuẩn hóa Unicode dựng sẵn: ă, â, đ, ê, ô, ơ, ư")
    assert not unicodedata.is_normalized("NFC", decomposed)

    broken_space = "Tie\u0302\u0301ng Vie\u0302\u0323t ho \u0300a bi\u0300nh"
    normalized = normalize_vietnamese_nfc(decomposed)
    assert is_vietnamese_nfc(normalized)
    assert unicodedata.is_normalized("NFC", normalized)
    assert "Tiếng Việt" in normalized

    repaired = normalize_vietnamese_nfc(broken_space)
    assert is_vietnamese_nfc(repaired)
    assert repaired == "Tiếng Việt hòa bình"

    assert detect_bad_encoding_or_garbled("") is True
    assert detect_bad_encoding_or_garbled("Văn bản \ufffd lỗi CMap") is True
    assert detect_bad_encoding_or_garbled("Chuyển đổi tài liệu Tiếng Việt thành công.") is False


def test_digital_pdf_to_docx_end_to_end(tmp_path: Path):
    in_pdf = tmp_path / "sample_vietnamese.pdf"
    out_docx = tmp_path / "output_vietnamese.docx"
    _build_sample_vietnamese_pdf(in_pdf)

    analyses = analyze_pdf(in_pdf)
    assert len(analyses) == 1
    assert analyses[0].kind == PageKind.DIGITAL_NATIVE

    result = convert_pdf_to_docx(in_pdf, out_docx, mode="editable", lang="vie")
    assert result.nfc_verified is True
    assert result.digital_pages == 1
    assert result.ocr_pages == 0
    assert out_docx.is_file() and out_docx.stat().st_size > 0

    with zipfile.ZipFile(out_docx, "r") as zf:
        names = set(zf.namelist())
        assert "[Content_Types].xml" in names
        assert "word/document.xml" in names
        assert "word/styles.xml" in names
        doc_xml_bytes = zf.read("word/document.xml")

    root = ET.fromstring(doc_xml_bytes)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    tables = root.findall(".//w:tbl", ns)
    assert len(tables) >= 1, "Expected at least one real <w:tbl> element in word/document.xml"

    drawings = root.findall(".//w:drawing", ns)
    assert len(drawings) >= 1, "Expected embedded image (<w:drawing>) in word/document.xml"

    all_text = "".join(t.text or "" for t in root.findall(".//w:t", ns))
    assert unicodedata.is_normalized("NFC", all_text)
    assert is_vietnamese_nfc(all_text)
    assert "BÁO CÁO KỸ THUẬT" in all_text
    assert "Phân tích bố cục" in all_text
    assert "ă, â, đ, ê, ô, ơ, ư" in all_text


def test_paddle_detector_and_scanned_pdf_vietocr_end_to_end(tmp_path: Path):
    digital_pdf = tmp_path / "digital.pdf"
    scanned_pdf = tmp_path / "scanned.pdf"
    out_docx = tmp_path / "scanned_out.docx"

    _build_sample_vietnamese_pdf(digital_pdf)
    _build_scanned_pdf_from_digital(digital_pdf, scanned_pdf)

    # Verify PaddleTextDetector detects 4-point polygons directly on rendered page image
    doc = pymupdf.open(str(scanned_pdf))
    pix = doc[0].get_pixmap(dpi=220, alpha=False)
    img_rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    image_bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
    doc.close()

    detector = PaddleTextDetector(use_angle_cls=True)
    regions = detector.detect_and_crop(image_bgr)
    assert len(regions) >= 5, f"Expected multiple PaddleOCR text regions, got {len(regions)}"
    assert regions[0].polygon.shape == (4, 2)

    analyses = analyze_pdf(scanned_pdf)
    assert len(analyses) == 1
    assert analyses[0].kind == PageKind.SCANNED_OR_BROKEN_FONT

    result = convert_pdf_to_docx(
        scanned_pdf,
        out_docx,
        mode="editable",
        lang="vie",
        ocr_dpi=220,
        ocr_model="vgg_transformer",
    )
    assert result.ocr_pages == 1
    assert result.nfc_verified is True
    assert out_docx.is_file() and out_docx.stat().st_size > 0

    with zipfile.ZipFile(out_docx, "r") as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    assert len(root.findall(".//w:tbl", ns)) >= 1, "Expected table structure recovered from scanned page"
    all_text = "".join(t.text or "" for t in root.findall(".//w:t", ns))
    assert unicodedata.is_normalized("NFC", all_text)
    assert is_vietnamese_nfc(all_text)
    # Check Vietnamese diacritics recognized accurately by VietOCR
    assert any(kw in all_text for kw in ("BÁO CÁO", "KỸ THUẬT", "Tiếng Việt", "Trạng thái"))


def test_cli_single_command_and_samples_output(tmp_path: Path):
    sample_pdf = SAMPLES_DIR / "sample_vietnamese.pdf"
    sample_docx = SAMPLES_DIR / "output_vietnamese.docx"
    _build_sample_vietnamese_pdf(sample_pdf)

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "convert.py"), str(sample_pdf), str(sample_docx)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, f"CLI failed: {proc.stderr}"
    assert sample_docx.is_file() and sample_docx.stat().st_size > 0
    assert "nfc_verified=True" in proc.stdout

    # Also verify fresh subprocess CLI execution on a scanned PDF (PaddleOCR before VietOCR)
    scanned_pdf = tmp_path / "cli_scanned.pdf"
    scanned_docx = tmp_path / "cli_scanned.docx"
    _build_scanned_pdf_from_digital(sample_pdf, scanned_pdf)
    proc_ocr = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "convert.py"),
            str(scanned_pdf),
            str(scanned_docx),
            "--dpi",
            "220",
            "--vietocr-model",
            "vgg_seq2seq",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc_ocr.returncode == 0, f"Scanned CLI failed: {proc_ocr.stderr}"
    assert scanned_docx.is_file() and scanned_docx.stat().st_size > 0
    assert "ocr=1" in proc_ocr.stdout
    assert "nfc_verified=True" in proc_ocr.stdout


def test_abbyy_install_directory_untouched_and_adapter_read_only():
    if ABBYY_DIR.is_dir():
        adapter = AbbyyLocalAdapter()
        assert adapter.is_available() is True
        export_wp = ABBYY_DIR / "export.wp.dll"
        digest = hashlib.sha256(export_wp.read_bytes()).hexdigest()
        assert digest == "aeb704c487d0112f17b3d3344f76d961faa672b251dc8c78eb1f04b5be83333b"


def test_adrt_paragraph_merge_and_typography_unit():
    bounds = (50.0, 50.0, 545.0, 780.0)
    l1 = LogicalLine(
        bbox=(74.0, 170.0, 540.0, 184.0),
        text="Hệ thống xử lý tài liệu tiếng Việt kết hợp nhận diện ký tự quang học",
        font_size_pt=12.0,
        is_bold=False,
        baseline_y=184.0,
    )
    l2 = LogicalLine(
        bbox=(50.0, 189.0, 538.0, 203.0),
        text="và chuẩn hóa Unicode dựng sẵn NFC phục vụ xuất bản word chất lượng cao.",
        font_size_pt=12.0,
        is_bold=False,
        baseline_y=203.0,
    )
    l3_list = LogicalLine(
        bbox=(50.0, 208.0, 420.0, 222.0),
        text="Điều 1. Quy định áp dụng đối với văn bản hành chính.",
        font_size_pt=12.0,
        is_bold=False,
        baseline_y=222.0,
    )
    l4_indent = LogicalLine(
        bbox=(76.0, 208.0, 540.0, 222.0),
        text="Đoạn văn mới thụt đầu dòng tách riêng biệt khỏi đoạn trước.",
        font_size_pt=12.0,
        is_bold=False,
        baseline_y=222.0,
    )
    l_short_end = LogicalLine(
        bbox=(50.0, 189.0, 290.0, 203.0),
        text="Kết thúc câu ngắn giữa dòng.",
        font_size_pt=12.0,
        is_bold=False,
        baseline_y=203.0,
    )

    assert should_merge_into_paragraph(l1, l2, bounds, median_line_height=14.0) is True
    assert should_merge_into_paragraph(l2, l3_list, bounds, median_line_height=14.0) is False
    assert should_merge_into_paragraph(l2, l4_indent, bounds, median_line_height=14.0) is False
    assert should_merge_into_paragraph(l_short_end, l2, bounds, median_line_height=14.0) is False


def _build_complex_adrt_vietnamese_pdf(pdf_path: Path) -> None:
    """Create a complex Vietnamese administrative/technical PDF with 2-col header,
    centered bold title, justified multi-line body paragraph with first-line indent,
    clause/list items, merged-cell bordered table, 2-col signature block & footer."""
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)

    times_regular = r"C:\Windows\Fonts\times.ttf"
    times_bold = r"C:\Windows\Fonts\timesbd.ttf"
    if os.path.exists(times_regular) and os.path.exists(times_bold):
        page.insert_font(fontname="F_REG", fontfile=times_regular)
        page.insert_font(fontname="F_BLD", fontfile=times_bold)
        f_reg, f_bld = "F_REG", "F_BLD"
    else:
        f_reg, f_bld = "helv", "hebo"

    # 1. Parallel 2-column administrative header (left & right blocks)
    page.insert_textbox(
        pymupdf.Rect(50, 58, 240, 78),
        "CÔNG TY CÔNG NGHỆ AI",
        fontname=f_bld,
        fontsize=10.5,
        align=pymupdf.TEXT_ALIGN_CENTER,
    )
    page.insert_textbox(
        pymupdf.Rect(50, 77, 240, 96),
        "Số: 16/2026/BC-KT",
        fontname=f_reg,
        fontsize=10.5,
        align=pymupdf.TEXT_ALIGN_CENTER,
    )

    page.insert_textbox(
        pymupdf.Rect(295, 58, 545, 78),
        "CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM",
        fontname=f_bld,
        fontsize=10.5,
        align=pymupdf.TEXT_ALIGN_CENTER,
    )
    page.insert_textbox(
        pymupdf.Rect(295, 77, 545, 96),
        "Độc lập - Tự do - Hạnh phúc",
        fontname=f_bld,
        fontsize=10.5,
        align=pymupdf.TEXT_ALIGN_CENTER,
    )

    # 2. Centered bold document title
    page.insert_textbox(
        pymupdf.Rect(50, 122, 545, 152),
        "BÁO CÁO KỸ THUẬT TÁI TẠO BỐ CỤC",
        fontname=f_bld,
        fontsize=15.0,
        align=pymupdf.TEXT_ALIGN_CENTER,
    )

    # 3. Multi-line justified paragraph with genuine ~24pt first-line indent wrapping to x=545
    long_justified_para = (
        "\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0"
        "Hệ thống tái tạo cấu trúc văn bản Tiếng Việt kết hợp PaddleOCR và VietOCR "
        "giúp chuẩn hóa toàn bộ dấu thanh điệu Unicode dựng sẵn NFC, đồng thời phân tích "
        "hình học bố cục để nối dòng tự nhiên thành một đoạn văn hoàn chỉnh như bản gốc."
    )
    page.insert_textbox(
        pymupdf.Rect(50, 168, 545, 232),
        long_justified_para,
        fontname=f_reg,
        fontsize=11.5,
        align=pymupdf.TEXT_ALIGN_JUSTIFY,
    )

    # 4. Separate Vietnamese list & clause lines (must not merge)
    page.insert_text((50, 245), "Điều 1. Bảo toàn định dạng văn bản hành chính.", fontname=f_reg, fontsize=11.5)
    page.insert_text((50, 267), "1. Giữ nguyên bảng biểu, gộp ô và căn lề chuẩn xác.", fontname=f_reg, fontsize=11.5)

    # 5. Bordered Table with col_span=2 merged header cell (Row 0, cols 1-2)
    x_cols = [50, 120, 355, 545]
    y_rows = [305, 340, 380, 420]
    shape = page.new_shape()
    for y in y_rows:
        shape.draw_line((x_cols[0], y), (x_cols[-1], y))
    # Outer vertical borders + inner divider at x=120 full height
    shape.draw_line((x_cols[0], y_rows[0]), (x_cols[0], y_rows[-1]))
    shape.draw_line((x_cols[1], y_rows[0]), (x_cols[1], y_rows[-1]))
    shape.draw_line((x_cols[3], y_rows[0]), (x_cols[3], y_rows[-1]))
    # Inner vertical divider at x=355 ONLY for rows 1 & 2 (omitted in row 0 -> merged col_span=2!)
    shape.draw_line((x_cols[2], y_rows[1]), (x_cols[2], y_rows[-1]))
    shape.finish(color=(0, 0, 0), width=1.2)
    shape.commit()

    page.insert_textbox(
        pymupdf.Rect(x_cols[0] + 4, y_rows[0] + 9, x_cols[1] - 4, y_rows[1] - 4),
        "STT",
        fontname=f_bld,
        fontsize=10.5,
        align=pymupdf.TEXT_ALIGN_CENTER,
    )
    page.insert_textbox(
        pymupdf.Rect(x_cols[1] + 4, y_rows[0] + 9, x_cols[3] - 4, y_rows[1] - 4),
        "Thông tin đánh giá chất lượng",
        fontname=f_bld,
        fontsize=10.5,
        align=pymupdf.TEXT_ALIGN_CENTER,
    )

    rows_body = [
        ("01", "Nhận diện chữ Tiếng Việt", "Hoạt động tốt"),
        ("02", "Gộp ô bảng biểu OpenXML", "Chính xác"),
    ]
    for r_i, (c0, c1, c2) in enumerate(rows_body, start=1):
        y0, y1 = y_rows[r_i] + 10, y_rows[r_i + 1] - 4
        page.insert_textbox(pymupdf.Rect(x_cols[0] + 4, y0, x_cols[1] - 4, y1), c0, fontname=f_reg, fontsize=10.5, align=pymupdf.TEXT_ALIGN_CENTER)
        page.insert_text((x_cols[1] + 8, y0 + 13), c1, fontname=f_reg, fontsize=10.5)
        page.insert_text((x_cols[2] + 8, y0 + 13), c2, fontname=f_reg, fontsize=10.5)

    # 6. Parallel 2-column footer signature block
    page.insert_text((50, 480), "Nơi nhận:", fontname=f_bld, fontsize=10.5)
    page.insert_text((50, 498), "- Ban Giám đốc;", fontname=f_reg, fontsize=10.0)
    page.insert_text((50, 516), "- Lưu: VT, Phòng R&D.", fontname=f_reg, fontsize=10.0)

    page.insert_textbox(
        pymupdf.Rect(340, 468, 545, 488),
        "GIÁM ĐỐC TRUNG TÂM",
        fontname=f_bld,
        fontsize=10.5,
        align=pymupdf.TEXT_ALIGN_CENTER,
    )
    page.insert_textbox(
        pymupdf.Rect(340, 488, 545, 506),
        "(Ký, ghi rõ họ tên)",
        fontname=f_reg,
        fontsize=10.0,
        align=pymupdf.TEXT_ALIGN_CENTER,
    )
    page.insert_textbox(
        pymupdf.Rect(340, 540, 545, 560),
        "Nguyễn Văn An",
        fontname=f_bld,
        fontsize=10.5,
        align=pymupdf.TEXT_ALIGN_CENTER,
    )

    # 7. Running page number footer inside bottom 6% band
    page.insert_textbox(
        pymupdf.Rect(240, 796, 355, 816),
        "Trang 1/1",
        fontname=f_reg,
        fontsize=9.5,
        align=pymupdf.TEXT_ALIGN_CENTER,
    )

    doc.save(str(pdf_path))
    doc.close()


def test_adrt_complex_layout_scanned_pdf_end_to_end(tmp_path: Path):
    digital_pdf = tmp_path / "complex_digital.pdf"
    scanned_pdf = tmp_path / "complex_scanned.pdf"
    out_docx = tmp_path / "complex_adrt_out.docx"

    _build_complex_adrt_vietnamese_pdf(digital_pdf)
    _build_scanned_pdf_from_digital(digital_pdf, scanned_pdf)

    result = convert_pdf_to_docx(
        scanned_pdf,
        out_docx,
        mode="editable",
        lang="vie",
        ocr_dpi=260,
        ocr_model="vgg_transformer",
    )
    assert result.ocr_pages == 1
    assert result.nfc_verified is True
    assert out_docx.is_file() and out_docx.stat().st_size > 0

    with zipfile.ZipFile(out_docx, "r") as zf:
        names = set(zf.namelist())
        assert "word/document.xml" in names
        doc_root = ET.fromstring(zf.read("word/document.xml"))
        footer_xml = zf.read("word/footer1.xml") if "word/footer1.xml" in names else b""

    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    w_val = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val"
    w_first_line = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}firstLine"

    # 1. Verify tables: at least 1 borderless parallel layout table (nil) and 1 bordered merged table (single + gridSpan)
    tables = doc_root.findall(".//w:tbl", ns)
    assert len(tables) >= 3, f"Expected >=3 tables (2 parallel layout tables + 1 bordered table), got {len(tables)}"

    nil_tables = []
    single_tables = []
    for tbl in tables:
        tc_borders = tbl.findall(".//w:tblBorders/w:top", ns)
        vals = [el.attrib.get(w_val) for el in tc_borders]
        if any(v in ("nil", "none") for v in vals):
            nil_tables.append(tbl)
        elif "single" in vals:
            single_tables.append(tbl)

    assert len(nil_tables) >= 2, f"Expected >=2 borderless ParallelColumnsBlock tables (header & signature), got {len(nil_tables)}"
    assert len(single_tables) >= 1, "Expected bordered ReconstructedTable"

    # Verify merged cell <w:gridSpan w:val="2"/> inside the bordered data table
    grid_spans = single_tables[0].findall(".//w:gridSpan", ns)
    assert any(el.attrib.get(w_val) == "2" for el in grid_spans), "Expected col_span=2 merged cell (<w:gridSpan w:val='2'/>)"

    # 2. Verify top-level body paragraphs: title centered + bold, 3-line body merged into 1 JUSTIFY+firstLine <w:p>, list items separate
    body = doc_root.find("w:body", ns)
    assert body is not None
    body_paras = body.findall("w:p", ns)

    def _p_text(p_el) -> str:
        return "".join(t.text or "" for t in p_el.findall(".//w:t", ns)).strip()

    title_paras = [p for p in body_paras if "BÁO CÁO" in _p_text(p)]
    assert len(title_paras) == 1, f"Expected centered title paragraph, got {[_p_text(p) for p in body_paras]}"
    title_jc = title_paras[0].find("./w:pPr/w:jc", ns)
    assert title_jc is not None and title_jc.attrib.get(w_val) == "center"
    assert title_paras[0].find(".//w:rPr/w:b", ns) is not None

    merged_body_paras = [p for p in body_paras if "Hệ thống" in _p_text(p)]
    assert len(merged_body_paras) == 1
    merged_text = _p_text(merged_body_paras[0])
    # All wrapped lines must be merged into a single paragraph (spanning line 1 -> line 2 -> line 3)
    assert "VietOCR" in merged_text and "NFC" in merged_text and "tự nhiên" in merged_text, (
        f"Expected 3-line paragraph merged cleanly, got: {merged_text!r}"
    )
    merged_jc = merged_body_paras[0].find("./w:pPr/w:jc", ns)
    assert merged_jc is not None and merged_jc.attrib.get(w_val) == "both"
    merged_ind = merged_body_paras[0].find("./w:pPr/w:ind", ns)
    assert merged_ind is not None and int(merged_ind.attrib.get(w_first_line, "0")) >= 150

    # Clause and numbered list items must stay distinct from merged_body_paras
    assert any("Điều 1" in _p_text(p) for p in body_paras)
    assert any("Giữ nguyên" in _p_text(p) for p in body_paras)

    # 3. Verify running page number footer routed to word/footer1.xml
    assert footer_xml, "Expected word/footer1.xml to be synthesized for bottom 6% footer"
    footer_root = ET.fromstring(footer_xml)
    footer_text = "".join(t.text or "" for t in footer_root.findall(".//w:t", ns))
    assert "Trang" in footer_text or "1/1" in footer_text, f"Expected page number in footer, got {footer_text!r}"


def test_deskew_red_seal_removal_and_admin_postprocessing():
    from pdf2word import (
        VietnameseAdminPostProcessor,
        deskew_image_bgr,
        is_browser_or_viewer_noise,
        remove_red_pixels_for_ocr,
    )

    # 1. Projection-variance deskew aligns synthetic tilted lines within [-3, +3] deg
    canvas = np.full((600, 800, 3), 255, dtype=np.uint8)
    for y in range(120, 480, 40):
        cv2.rectangle(canvas, (100, y), (700, y + 10), (20, 20, 20), -1)
    rot_m = cv2.getRotationMatrix2D((400, 300), -1.8, 1.0)
    skewed = cv2.warpAffine(canvas, rot_m, (800, 600), borderValue=(255, 255, 255))
    fixed, applied = deskew_image_bgr(skewed)
    assert abs(applied) >= 1.2
    assert fixed.shape == skewed.shape

    # 2. Red circular seal removal whitens large round red stamps while keeping black text & small red stamps
    page_bgr = np.full((800, 800, 3), 255, dtype=np.uint8)
    # Circular red seal (diameter ~220px) overlapping black horizontal text bar
    cv2.rectangle(page_bgr, (260, 470), (540, 495), (10, 10, 10), -1)
    cv2.circle(page_bgr, (400, 500), 110, (30, 30, 210), 6)
    # Wide small red header stamp ("MẬT" box)
    cv2.rectangle(page_bgr, (60, 80), (220, 120), (30, 30, 210), 3)
    cleaned = remove_red_pixels_for_ocr(page_bgr)
    # Circular red ring pixels erased to near-white; black intersecting text and horizontal red stamp preserved
    assert tuple(int(v) for v in cleaned[390, 400]) == (255, 255, 255)
    assert int(cleaned[482, 300].mean()) < 40
    assert int(cleaned[80, 120, 2]) > 180 and int(cleaned[80, 120, 0]) < 60

    # 3. Reading order X-overlap guard avoids reversing slightly tilted stacked full-width lines
    line1 = np.array([[450, 2149], [2254, 2165], [2254, 2285], [450, 2269]], dtype=np.float32)
    line2 = np.array([[314, 2218], [2247, 2234], [2247, 2358], [314, 2342]], dtype=np.float32)
    ordered = sort_polygons_reading_order([line2, line1])
    assert np.allclose(ordered[0], line1)
    assert np.allclose(ordered[1], line2)

    # 4. Vietnamese administrative post-processor & noise filters
    assert is_browser_or_viewer_noise("PDF.js viewer", rel_yc=0.02) is True
    assert is_browser_or_viewer_noise("16:18 1/9/26 Kế hoạch", rel_yc=0.02) is True
    assert is_browser_or_viewer_noise("https://vbdh.example/doc", rel_yc=0.96) is True
    assert is_browser_or_viewer_noise("2", rel_yc=0.97) is True
    assert is_browser_or_viewer_noise("Trang 1/1", rel_yc=0.97) is False

    assert VietnameseAdminPostProcessor.clean_line("Độc lập ? Tự do ? Hạnh phúc") == "Độc lập - Tự do - Hạnh phúc"
    assert VietnameseAdminPostProcessor.clean_line("L. MỤC ĐÍCH, YÊU CẦU") == "I. MỤC ĐÍCH, YÊU CẦU"
    assert VietnameseAdminPostProcessor.clean_line("IL NỘI DUNG") == "II. NỘI DUNG"
    assert VietnameseAdminPostProcessor.clean_line("xin ý kiến chỉ đạo./ Tayl") == "xin ý kiến chỉ đạo./."
    assert VietnameseAdminPostProcessor.clean_line("- Súng Tiểu liên AK: 05 viên X 180 đ%c? 1.440 viên.") == "- Súng Tiểu liên AK: 05 viên x 180 đ/c = 1.440 viên."
    parts = VietnameseAdminPostProcessor.split_inline_subitems("1.1. Đ/c Nguyễn Văn A Thành 1,2. Đ/c Trần Văn B")
    assert len(parts) == 2 and parts[0].startswith("1.1.") and parts[1].startswith("1.2.")
