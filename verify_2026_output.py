"""End-to-end structural and content verification script for samples/2026.docx."""

from __future__ import annotations

import re
import sys
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

from pdf2word.vietnamese import is_vietnamese_nfc


def _cell_text_and_drawings(cell) -> tuple[list[str], int]:
    texts = [p.text.strip() for p in cell.paragraphs if p.text.strip()]
    drawings = len(cell._tc.findall(f".//{qn('w:drawing')}"))
    return texts, drawings


def verify_2026_docx(docx_path: Path) -> int:
    assert docx_path.is_file() and docx_path.stat().st_size > 0, f"Missing or empty: {docx_path}"

    with zipfile.ZipFile(docx_path, "r") as zf:
        names = set(zf.namelist())
        doc_xml = zf.read("word/document.xml")
        hdr_xml = zf.read("word/header1.xml") if "word/header1.xml" in names else b""
        ftr_xml = zf.read("word/footer1.xml") if "word/footer1.xml" in names else b""

    ns = {
        "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        "v": "urn:schemas-microsoft-com:vml",
    }
    doc_root = ET.fromstring(doc_xml)
    all_t = "".join(t.text or "" for t in doc_root.findall(".//w:t", ns))
    hdr_t = "".join(t.text or "" for t in ET.fromstring(hdr_xml).findall(".//w:t", ns)) if hdr_xml else ""
    ftr_t = "".join(t.text or "" for t in ET.fromstring(ftr_xml).findall(".//w:t", ns)) if ftr_xml else ""

    doc = Document(str(docx_path))

    # Dump ordered flow elements for visual inspection
    para_map = {p._p: p for p in doc.paragraphs}
    tbl_map = {t._tbl: t for t in doc.tables}
    ordered_paras: list[str] = []
    ordered_para_objs = []

    print(f"Inspecting: {docx_path} ({len(doc.tables)} tables, {len(doc.paragraphs)} body paragraphs)")
    print(f"Header XML text: {hdr_t!r}")
    print(f"Footer XML text: {ftr_t!r}")

    elem_idx = 0
    for child in doc.element.body:
        if child in para_map:
            p = para_map[child]
            txt = p.text.strip()
            drawings = len(child.findall(f".//{qn('w:drawing')}"))
            if txt or drawings:
                ordered_paras.append(txt)
                ordered_para_objs.append(p)
                al = str(p.alignment).split(".")[-1] if p.alignment is not None else "NONE"
                print(f"[P {elem_idx:03d}] align={al:<7} img={drawings} | {txt}")
                elem_idx += 1
        elif child in tbl_map:
            t = tbl_map[child]
            print(f"[T {elem_idx:03d}] rows={len(t.rows)} cols={len(t.columns)}")
            for r_i, row in enumerate(t.rows):
                for c_i, cell in enumerate(row.cells):
                    ctexts, cimgs = _cell_text_and_drawings(cell)
                    print(f"   ({r_i},{c_i}) img={cimgs} -> {ctexts}")
                    for ct in ctexts:
                        ordered_paras.append(ct)
            elem_idx += 1

    all_joined = "\n".join(ordered_paras)
    errors: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    # Unicode NFC check
    check(unicodedata.is_normalized("NFC", all_t), "document.xml is not Unicode NFC normalized")
    check(is_vietnamese_nfc(all_t), "document.xml contains stray uncomposed combining diacritics")

    # 1. Page 1 Header Table, Floating TextBoxes & Title
    check("CỤC C10" not in hdr_t, f"CỤC C10 leaked into section header: {hdr_t!r}")

    shapes = doc_root.findall(".//v:shape", ns)
    mat_shapes = []
    banso_shapes = []
    for sh in shapes:
        txbx_txt = "".join(t.text or "" for t in sh.findall(".//w:txbxContent//w:t", ns)).strip()
        if txbx_txt == "MẬT":
            mat_shapes.append(sh)
        elif "BẢN SỐ" in txbx_txt.upper():
            banso_shapes.append(sh)
    check(len(mat_shapes) >= 1, f"Expected floating VML TextBox for 'MẬT', got {len(mat_shapes)}")
    for sh in mat_shapes:
        check(sh.attrib.get("stroked") == "t", f"'MẬT' TextBox must have stroked='t', got {sh.attrib.get('stroked')!r}")
        sz_vals = [el.attrib.get(qn("w:val")) for el in sh.findall(".//w:txbxContent//w:rPr/w:sz", ns)]
        check("26" in sz_vals, f"'MẬT' TextBox expected 13pt (w:sz=26), got {sz_vals}")
        check(sh.find(".//w:txbxContent//w:rPr/w:b", ns) is not None, "'MẬT' TextBox expected bold <w:b/>")
    check(len(banso_shapes) >= 1, f"Expected floating VML TextBox for 'BẢN SỐ', got {len(banso_shapes)}")
    for sh in banso_shapes:
        check(sh.attrib.get("stroked") == "f", f"'BẢN SỐ' TextBox must have stroked='f', got {sh.attrib.get('stroked')!r}")

    check(len(doc.tables) >= 4, f"Expected >= 4 parallel tables, got {len(doc.tables)}")
    if doc.tables:
        t0 = doc.tables[0]
        check(len(t0.rows) >= 1 and len(t0.columns) == 2, f"Expected 2-column Table 0, got {len(t0.rows)}x{len(t0.columns)}")
        if len(t0.columns) == 2:
            left_lines_t0 = [p.text.strip() for p in t0.rows[0].cells[0].paragraphs if p.text.strip()]
            right_lines_t0 = [p.text.strip() for p in t0.rows[0].cells[1].paragraphs if p.text.strip()]
            left_t0 = "\n".join(left_lines_t0)
            right_t0 = "\n".join(right_lines_t0)
            check(bool(left_lines_t0) and "CỤC C10" in left_lines_t0[0], f"Table 0 row 0 left[0] must be 'CỤC C10': {left_lines_t0!r}")
            check(
                bool(right_lines_t0) and "VIỆT NAM" in right_lines_t0[0],
                f"Table 0 row 0 right[0] must be Quốc hiệu: {right_lines_t0!r}",
            )
            check("CỤC C10" in left_t0, f"Missing 'CỤC C10' in Table 0 left cell: {left_t0!r}")
            check("TRẠI GIAM KÊNH 5" in left_t0, f"Missing 'TRẠI GIAM KÊNH 5' in Table 0 left cell: {left_t0!r}")
            check("646" in left_t0, f"Missing 'Số: 646/KH-K5-Đ5' in Table 0 left cell: {left_t0!r}")
            check("HÒA XÃ HỘI" in right_t0 and "VIỆT NAM" in right_t0, f"Missing Quốc hiệu in Table 0 right cell: {right_t0!r}")
            check("Độc lập - Tự do - Hạnh phúc" in right_t0, f"Missing Tiêu ngữ in Table 0 right cell: {right_t0!r}")
            check("Cần Thơ" in right_t0, f"Missing date line in Table 0 right cell: {right_t0!r}")
            check("BẢN SỐ" not in left_t0.upper() and "BẢN SỐ" not in right_t0.upper(), "'BẢN SỐ' leaked into Table 0")
            check("MẬT" not in left_lines_t0 and "MẬT" not in right_lines_t0, "'MẬT' leaked into Table 0")

    border_sides = ("top", "left", "bottom", "right", "insideH", "insideV")
    tc_sides = ("top", "left", "bottom", "right")
    for t_idx, tbl_obj in enumerate(doc.tables):
        if len(tbl_obj.columns) == 2:
            tbl_el = tbl_obj._tbl
            for side in border_sides:
                el = tbl_el.find(f"./w:tblPr/w:tblBorders/w:{side}", ns)
                val = el.attrib.get(qn("w:val")) if el is not None else None
                check(val == "none", f"Table {t_idx} tblBorders/w:{side} expected 'none', got {val!r}")
            for tc_el in tbl_el.findall(".//w:tc", ns):
                for side in tc_sides:
                    el = tc_el.find(f"./w:tcPr/w:tcBorders/w:{side}", ns)
                    val = el.attrib.get(qn("w:val")) if el is not None else None
                    check(val == "none", f"Table {t_idx} tcBorders/w:{side} expected 'none', got {val!r}")

    check(
        any("KẾ HOẠCH" in p.text and "CENTER" in str(p.alignment) for p in ordered_para_objs[:8]),
        "Expected centered 'KẾ HOẠCH' body paragraph below Table 0",
    )

    # Opening paragraph continuity (missing middle line check)
    check(
        "tập huấn điều lệnh, huấn luyện quân sự, võ thuật Công an nhân dân (CAND) năm" in all_joined
        or ("tập huấn điều lệnh" in all_joined and "Công an nhân dân (CAND)" in all_joined),
        "Missing opening paragraph middle line containing 'tập huấn điều lệnh... Công an nhân dân (CAND)'",
    )

    # Page 1 Muc I.2 ordering check
    idx_trong = all_joined.find("Trong thời gian tổ chức")
    idx_chap = all_joined.find("điều lệnh CAND")
    check(idx_trong != -1, "Missing Mục I.2 first line 'Trong thời gian tổ chức...'")
    if idx_trong != -1 and idx_chap != -1:
        check(idx_trong < idx_chap, f"Mục I.2 line order reversed: 'Trong thời gian' ({idx_trong}) after 'điều lệnh CAND' ({idx_chap})")

    # 3. Page 3 Muc V items 2.-8. & bullets
    for marker in ("2.", "3.", "4.", "5.", "6.", "7.", "8."):
        check(re.search(rf"(?m)^\s*{re.escape(marker)}\s+", all_joined) is not None, f"Missing Mục V item '{marker}'")
    check("CZ83" in all_joined and "1.560 viên" in all_joined, "Missing CZ83 ammo bullet or '= 1.560 viên'")
    check("1.440 viên" in all_joined, "Missing AK ammo bullet '= 1.440 viên'")
    check("Tổng các loại đạn: 3.000 viên" in all_joined, "Missing Mục V.6 total ammo bullet")
    check("Dự trù đạn hư hỏng: 600 viên" in all_joined, "Missing Mục V.6 damaged ammo bullet")
    check("Bia số 4A" in all_joined and "02 cái" in all_joined, "Missing Mục V.4 item content ('Bia số 4A' / 'Bàn: 02 cái')")

    # 4. Page 4 Muc VII.3 & inline sub-item splits
    for kw in ("Lê Minh Cảnh", "Trần Văn Nhẫn", "Trương Hoàng Thức", "Lê Hoài Hận", "Lê Minh Cần"):
        check(kw in all_joined, f"Missing Đợt 1 officer '{kw}'")
    for kw in ("Dương Minh To", "Lê Văn Tấn", "Quang Văn Hậu"):
        check(kw in all_joined, f"Missing Đợt 2 officer '{kw}'")

    # Sub-items 1.1 & 1.2, 6.1 & 6.2 must not remain merged inside the same paragraph
    for p_txt in ordered_paras:
        check(
            not (re.search(r"\b1\.1[\.,]", p_txt) and re.search(r"\b1\.2[\.,]", p_txt)),
            f"Sub-items 1.1 & 1.2 stayed merged in one paragraph: {p_txt!r}",
        )
        check(
            not (re.search(r"\b6\.1[\.,]", p_txt) and re.search(r"\b6\.2[\.,]", p_txt)),
            f"Sub-items 6.1 & 6.2 stayed merged in one paragraph: {p_txt!r}",
        )

    # 5. Typography, Orphan Tails, Noise & Seals
    def _run_sz_vals(p_obj) -> list[str]:
        return [
            el.attrib.get(qn("w:val"), "")
            for el in p_obj._p.findall(".//w:rPr/w:sz", ns)
        ]

    def _has_bold(p_obj) -> bool:
        return p_obj._p.find(".//w:rPr/w:b", ns) is not None

    def _has_italic(p_obj) -> bool:
        return p_obj._p.find(".//w:rPr/w:i", ns) is not None

    ke_hoach_paras = [p for p in ordered_para_objs[:8] if p.text.strip() == "KẾ HOẠCH"]
    check(len(ke_hoach_paras) == 1, f"Expected single 'KẾ HOẠCH' title para, got {len(ke_hoach_paras)}")
    for kp in ke_hoach_paras:
        check("32" in _run_sz_vals(kp) and _has_bold(kp), f"'KẾ HOẠCH' expected 16pt (32) Bold, got sz={_run_sz_vals(kp)}, bold={_has_bold(kp)}")

    trich_yeu_paras = [p for p in ordered_para_objs[:10] if "Kiểm tra bắn đạn thật" in p.text]
    check(bool(trich_yeu_paras), "Missing Trích yếu subtitle 'Kiểm tra bắn đạn thật...'")
    for tp in trich_yeu_paras:
        li = tp.paragraph_format.left_indent.pt if tp.paragraph_format.left_indent else 0.0
        ri = tp.paragraph_format.right_indent.pt if tp.paragraph_format.right_indent else 0.0
        check(
            "28" in _run_sz_vals(tp) and _has_bold(tp) and "CENTER" in str(tp.alignment) and li > 0 and ri > 0,
            f"Trích yếu styling mismatch: sz={_run_sz_vals(tp)}, bold={_has_bold(tp)}, align={tp.alignment}, L/R={li}/{ri}",
        )

    for p_obj in ordered_para_objs:
        ptxt = p_obj.text.strip()
        if not ptxt or ptxt == "KẾ HOẠCH" or "Kiểm tra bắn đạn thật" in ptxt:
            continue
        szs = _run_sz_vals(p_obj)
        check("28" in szs, f"Body paragraph expected 14pt (w:sz=28), got {szs} on {ptxt[:50]!r}")
        if re.match(r"^(?:[IVX]+\.\s|\*\s*(?:Tình huống|Biện pháp xử lý)|1\.\s*(?:Mục đích|Bắn bài 1))", ptxt):
            check(_has_bold(p_obj), f"Expected bold on heading/trigger paragraph: {ptxt[:60]!r}")

    if doc.tables:
        date_paras = [p for p in doc.tables[0].rows[0].cells[1].paragraphs if "Cần Thơ" in p.text]
        check(bool(date_paras) and all(_has_italic(p) for p in date_paras), "Expected Italic on 'Cần Thơ, ngày...'")

    all_table_paras = [p for t in doc.tables for row in t.rows for cell in row.cells for p in cell.paragraphs]
    noi_nhan_seen = False
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                c_paras = [p for p in cell.paragraphs if p.text.strip()]
                if any(p.text.strip().startswith("Nơi nhận") for p in c_paras):
                    noi_nhan_seen = True
                    in_recip = False
                    for cp in c_paras:
                        ctxt = cp.text.strip()
                        if ctxt.startswith("Nơi nhận"):
                            in_recip = True
                            check(
                                "24" in _run_sz_vals(cp) and _has_bold(cp) and _has_italic(cp),
                                f"'Nơi nhận:' expected 12pt (24) Bold+Italic, got sz={_run_sz_vals(cp)}, b={_has_bold(cp)}, i={_has_italic(cp)}",
                            )
                        elif in_recip:
                            check("22" in _run_sz_vals(cp), f"Recipient item expected 11pt (22), got {_run_sz_vals(cp)} on {ctxt!r}")
                            check(ctxt.startswith("- "), f"Recipient item expected '- ' prefix: {ctxt!r}")
    check(noi_nhan_seen, "Missing 'Nơi nhận:' block in footer tables")

    for orphan_tail, full_context in (
        ("vụ công tác kiểm tra.", "vụ công tác kiểm tra"),
        ("ban trường bắn.", "ban trường bắn"),
        ("Sĩ quan nghiệp vụ làm chỉ huy.", "Sĩ quan nghiệp vụ làm chỉ huy"),
    ):
        check(
            not any(p.strip() == orphan_tail for p in ordered_paras),
            f"Orphan tail remained a standalone paragraph: {orphan_tail!r}",
        )
        check(full_context in all_joined, f"Expected merged text missing: {full_context!r}")

    for forbidden in (
        "PDF.js",
        "vjewer",
        "https://",
        "vbđh",
        "hanh/nh",
        "16:18 1/9/26",
        "Tayl",
        "qigl",
        "qyl",
        "(igl",
        "Ville",
        "chiên đầu",
        "tuyệt đội",
        "cỏ hộp",
        "hồ dân",
        "đan cấp",
        "Xe chuyên dung",
        "QLHL8",
        "phôi hợp",
        "kiếm tra",
        "tống hợp",
        "trường băn",
        "[để thực hiện)",
        "hành vì",
        "vi pham",
        "pham vi",
        "kip thời",
        "bộ phân",
        "Bộ Công an.quy định",
        "trường giáo dường",
        "kiêm soát",
        "lao động lê",
        "01 quyền",
        "đánh bất cá",
    ):
        check(forbidden.lower() not in all_joined.lower(), f"Forbidden noise token survived: {forbidden!r}")
    check("đồng chí giám thị" not in all_joined, "Lowercase 'đồng chí giám thị' typo survived")
    check("Xe chuyên dung" not in all_joined, "Case typo 'Xe chuyên dung' survived")

    for isolated_noise in ("37", "ƯNG"):
        check(
            not any(p == isolated_noise for p in ordered_paras),
            f"Isolated grayscale seal OCR fragment survived as paragraph: {isolated_noise!r}",
        )

    # Seal drawings embedded
    total_drawings = len(doc_root.findall(".//w:drawing", ns))
    check(total_drawings >= 2, f"Expected >= 2 embedded seal/signature drawings (P5 & P8), got {total_drawings}")

    # PHÓ GIÁM THỊ presence without bogus short fragments inside signature cells
    pho_gt_count = sum(1 for p in ordered_paras if "PHÓ GIÁM THỊ" in p)
    check(pho_gt_count >= 2, f"Expected >= 2 'PHÓ GIÁM THỊ' signature headers, got {pho_gt_count}")

    if errors:
        print("\n=== VERIFICATION FAILURES ===")
        for e in errors:
            print(f"FAIL: {e}")
        return 1

    print("\nALL 2026.DOCX VERIFICATION CHECKS PASSED.")
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"D:\Project\PDFtoWord\samples\2026.docx")
    sys.exit(verify_2026_docx(target))
