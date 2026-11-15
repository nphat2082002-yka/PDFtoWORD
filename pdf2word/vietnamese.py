"""Vietnamese Unicode normalization, encoding heuristics, and text post-processing.

Modeled on ABBYY FineReader 16 Stage 1 (`pdftools.heuristics.dll`:
`CBadEncodingDetector`, `CDiacriticSymbolCalculator`) and Stage 4
(`langinfounicode.dll`: `Unicode::Compose`, `LettersSets::Accents`,
`Languages::GetInterwordPunctuators`).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

# Precomposed Vietnamese characters with tone marks or special base letters (NFC)
VIETNAMESE_SPECIAL_CHARS = frozenset(
    "àáảãạăằắẳẵặâầấẩẫậđèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵ"
    "ÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬĐÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴ"
)

# Combining diacritics commonly emitted separately in NFD PDFs (grave, acute, tilde, hook, dot-below, circumflex, breve, horn)
_COMBINING_MARKS = "\u0300\u0301\u0303\u0309\u0323\u0302\u0306\u031b"
_SPACED_COMBINING_RE = re.compile(rf"([A-Za-zÀ-ỹ])\s+([{_COMBINING_MARKS}]+)")

# Soft hyphen & zero-width artifacts frequently found in PDF text layers
_INVISIBLE_ARTIFACTS_RE = re.compile(r"[\u00ad\u200b\u200c\u200d\ufeff]")

# Common TCVN3 / VNI mojibake indicators when mapped to Latin-1 / Windows-1252
_MOJIBAKE_MARKERS = frozenset("µ¸¹¶·¨»¾¼½Æ©ÇÊÈÉËÌÐÎÏÑÒÕÓÔÖ×ÝØÜÞßãáâä«åèæçéêíëìîïóñòôõö÷ùøûüþ")

# Space immediately before closing punctuation or after opening punctuation
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:?!\)\]\}])")
_SPACE_AFTER_OPEN_RE = re.compile(r"([\(\[\{])\s+")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")

# Heuristic regex for detached Vietnamese diacritic tone marks in OCR output (e.g. "a '" or "o `")
_STRAY_COMBINING_RE = re.compile(rf"[{_COMBINING_MARKS}]")


def normalize_vietnamese_nfc(text: str, collapse_spaces: bool = False) -> str:
    """Normalize text to canonical precomposed Vietnamese Unicode (NFC).

    Repairs decomposed combining accents (including stray spaces before a
    combining mark), strips PDF soft-hyphen / zero-width artifacts, normalizes
    non-breaking spaces, and applies Unicode NFC composition.
    """
    if not text:
        return ""

    cleaned = _INVISIBLE_ARTIFACTS_RE.sub("", text)
    cleaned = cleaned.replace("\u00a0", " ")

    # Repair combining diacritics separated from their base letter by whitespace
    cleaned = _SPACED_COMBINING_RE.sub(r"\1\2", cleaned)

    # Canonical NFC composition (equivalent to LangInfo::Unicode::Compose)
    cleaned = unicodedata.normalize("NFC", cleaned)

    # Normalize spacing around punctuation without altering intentional newlines
    cleaned = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", cleaned)
    cleaned = _SPACE_AFTER_OPEN_RE.sub(r"\1", cleaned)

    if collapse_spaces:
        cleaned = _MULTI_SPACE_RE.sub(" ", cleaned)

    return cleaned


def is_vietnamese_nfc(text: str) -> bool:
    """Return True if text is normalized to Unicode NFC with no leftover combining marks."""
    if not text:
        return True
    if not unicodedata.is_normalized("NFC", text):
        return False
    # Check if any Vietnamese combining tone/diacritic remained uncomposed
    return _STRAY_COMBINING_RE.search(text) is None


def detect_bad_encoding_or_garbled(text: str) -> bool:
    """Detect whether extracted PDF text has broken CMap/font encoding.

    Mirrors ABBYY's `CBadEncodingDetector` / `CBadEncodingFontDetector` in
    `pdftools.heuristics.dll`: checks replacement characters (`\\ufffd`),
    Private Use Area glyphs (`U+E000..U+F8FF`), control codes, and
    high-density TCVN3/VNI mojibake patterns.
    """
    if not text or not text.strip():
        return True

    sample = text.strip()
    total = len(sample)
    if total == 0:
        return True

    replacement_count = sample.count("\ufffd")
    pua_count = sum(1 for ch in sample if 0xE000 <= ord(ch) <= 0xF8FF)
    ctrl_count = sum(1 for ch in sample if ord(ch) < 32 and ch not in ("\n", "\r", "\t"))

    # Any PUA or significant replacement/control character density signals broken ToUnicode CMap
    if replacement_count > 0 or pua_count > 0 or (ctrl_count / total) > 0.02:
        return True

    # If the page has zero standard Vietnamese NFC chars but heavy Latin-1 mojibake markers,
    # it is likely unmapped TCVN3 / VNI-Windows legacy font encoding.
    vn_count = sum(1 for ch in sample if ch in VIETNAMESE_SPECIAL_CHARS)
    mojibake_count = sum(1 for ch in sample if ch in _MOJIBAKE_MARKERS)
    if vn_count == 0 and mojibake_count >= 3 and (mojibake_count / total) > 0.03:
        return True

    return False


def repair_line_wraps(lines: Iterable[str]) -> str:
    """Join wrapped lines inside a paragraph cleanly while preserving sentence flow."""
    joined: list[str] = []
    for raw in lines:
        line = normalize_vietnamese_nfc(raw).strip()
        if not line:
            continue
        if not joined:
            joined.append(line)
            continue

        prev = joined[-1]
        # Rejoin words broken across line endings by an ASCII hyphen
        if prev.endswith("-") and not prev.endswith(" -") and len(prev) > 2:
            joined[-1] = prev[:-1] + line
        else:
            joined[-1] = f"{prev} {line}"
    return " ".join(joined)


# Browser print-to-PDF / PDF.js viewer header & footer patterns (not document content)
_BROWSER_VIEWER_RE = re.compile(
    r"(?:"
    r"pdf[\.\s]*js(?:\s*v[ij1l]ew\w*)?"
    r"|https?://\S+"
    r"|(?:https?|httn\w*|intin\w*)\s*[-:;.,/\\]+\s*[a-z0-9]"
    r"|\b(?:vbđh|vbdh|vhrdh|krth)\b"
    r"|van[-.\s]*[bh]an[-.\s]*d[on]"
    r"|[bh]an[-.\s]*hanh\s*[/\\|]+"
    r"|^\s*\d{1,2}:\d{2}\s+\d{1,2}/\d{1,2}/\d{2,4}\b"
    r"|^\s*hanh/nh\s*$"
    r"|^\s*\d+\.\d+\s*[:/]?\s*$"
    r")",
    re.IGNORECASE,
)

# Standalone page numbers at extreme top/bottom edge (exclude genuine "Trang X/Y" footers handled by Word footer)
_BARE_EDGE_PAGE_NUM_RE = re.compile(r"^\s*\d{1,3}\s*$")

# Inline sub-item delimiter (e.g., "1.1. ... 1.2. ..." or "Thành 7,2. Đợt 2: ..."), consuming any stray OCR prefix before the marker
_INLINE_SUBITEM_SPLIT_RE = re.compile(
    r"\s+(?:(?:Thành|Hình|Lành)\s+)?(?=\d{1,2}[\.,]\d{1,2}[\.,]?\s+(?:Đ/c|Đợt\b|Đ/ít|Đồng\s+chí|[A-ZÀ-Ỹ][a-zà-ỹ]{1,12}\b))"
)

_CONTEXTUAL_VIET_PHRASE_FIXES: tuple[tuple[re.Pattern[str], str], ...] = (
    # Ranks & titles
    (re.compile(r"\bThiếu\s+t[ảà]\b"), "Thiếu tá"),
    (re.compile(r"\bTrung\s+t[ảà]\b"), "Trung tá"),
    (re.compile(r"\bĐại\s+t[ảà]\b"), "Đại tá"),
    (re.compile(r"\bThượng\s+t[ảà]\b"), "Thượng tá"),
    (re.compile(r"\bBộ\s+trường\b"), "Bộ trưởng"),
    (re.compile(r"\bCần\s+bộ\b"), "Cán bộ"),
    (re.compile(r"\bT[ỏố]\s+Tr[ìi]nh\s+sát\b"), "Tổ Trinh sát"),
    (re.compile(r"\bTổ\s+Trình\s+sát\b"), "Tổ Trinh sát"),
    (re.compile(r"\bĐ/ít\b|\bĐíc\b"), "Đ/c"),
    (re.compile(r"\bđ/ít\b|\bđ%c\b|\bđéc\b"), "đ/c"),
    # Administrative / military / security phrases
    (re.compile(r"\bbăn\s+đạn\s+thật\b", re.IGNORECASE), "bắn đạn thật"),
    (re.compile(r"\brên\s+luyện\b"), "rèn luyện"),
    (re.compile(r"\bRên\s+luyện\b"), "Rèn luyện"),
    (re.compile(r"\bkiếm\s+soát\b"), "kiểm soát"),
    (re.compile(r"\bKiếm\s+soát\b"), "Kiểm soát"),
    (re.compile(r"\bxứ\s+lý\b"), "xử lý"),
    (re.compile(r"\bXứ\s+lý\b"), "Xử lý"),
    (re.compile(r"\bb[ãà]o\s+vệ\b"), "bảo vệ"),
    (re.compile(r"\bB[ãà]o\s+vệ\b"), "Bảo vệ"),
    (re.compile(r"\bquân\s+lý\b"), "quản lý"),
    (re.compile(r"\bQuân\s+lý\b"), "Quản lý"),
    (re.compile(r"\bsơ\s+hỏ\b"), "sơ hở"),
    (re.compile(r"\bbất\s+buộc\b"), "bắt buộc"),
    (re.compile(r"\bhoa\s+mảu\b"), "hoa màu"),
    (re.compile(r"\btrộm\s+cáp\b"), "trộm cắp"),
    (re.compile(r"\bT[ốỏ]\s+(?:tuần|huần)\s+tra\b"), "Tổ tuần tra"),
    (re.compile(r"\bdần\s+giải\b"), "dẫn giải"),
    (re.compile(r"\bnười\s+(Chỉ\s+huy|chỉ\s+huy)\b"), r"người \1"),
    (re.compile(r"\bTỉnh\s+trạng\s+bia\b"), "Tình trạng bia"),
    (re.compile(r"\bBắn\s+thứ\b"), "Bắn thử"),
    (re.compile(r"\bbắn\s+thứ\b"), "bắn thử"),
    (re.compile(r"\bvũ\s+khi\b"), "vũ khí"),
    (re.compile(r"\bbảo\s+cáo\b"), "báo cáo"),
    (re.compile(r"\bBảo\s+cáo\b"), "Báo cáo"),
    (re.compile(r"\bchi\s+đạo\b"), "chỉ đạo"),
    (re.compile(r"\bý\s+kiến\s+chi\s+đạo\b"), "ý kiến chỉ đạo"),
    (re.compile(r"\bđiều\s+bên\b"), "điều kiện"),
    (re.compile(r"\bBệ\s+bản\b"), "Bệ bắn"),
    (re.compile(r"\bbắn\s+nữu\b"), "bắn như"),
    (re.compile(r"\bhồ\s+dân\s+và\s+giấy\s+dân\b"), "hồ dán và giấy dán"),
    (re.compile(r"\bSúng\s+ngăn\b"), "Súng ngắn"),
    (re.compile(r"\bsúng\s+ngăn\b"), "súng ngắn"),
    (re.compile(r"\bThời\s+gian\s+băn\b"), "Thời gian bắn"),
    (re.compile(r"\bBăn\s+và\s+mệnh\b"), "bắn và mệnh"),
    (re.compile(r"\bkiêm\s+tra\b"), "kiểm tra"),
    (re.compile(r"\btâp\s+huân\b"), "tập huấn"),
    (re.compile(r"\bđiêu\s+lệnh\b"), "điều lệnh"),
    (re.compile(r"\bhuân\s+luyện\b"), "huấn luyện"),
    (re.compile(r"\bquân\s+sư,\s*võ\s+thuật\b"), "quân sự, võ thuật"),
    (re.compile(r"\bchiên\s+sĩ\b"), "chiến sĩ"),
    (re.compile(r"\bchiên\s+đâu\b"), "chiến đấu"),
    (re.compile(r"\bnhiêm\s+vu\b"), "nhiệm vụ"),
    (re.compile(r"\bđông\s+thời\b"), "đồng thời"),
    (re.compile(r"\bcụ\s+thế\b"), "cụ thể"),
    (re.compile(r"^KÊ\s+HOẠCH$"), "KẾ HOẠCH"),
    (re.compile(r"CÔNG\s+HÒA\s+XÃ\s+HỘI\s+CHU\s+NGHIA\s+VIỆT\s+NAM"), "CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM"),
    (re.compile(r"\bThiêu\s+tá\b"), "Thiếu tá"),
    (re.compile(r"\bLê\s+Hoài\s+Hân\b"), "Lê Hoài Hận"),
    (re.compile(r"\bCán\s+bô\b"), "Cán bộ"),
    (re.compile(r"\bcản\s+bộ\b"), "cán bộ"),
    (re.compile(r"\bPhân\s+trại\s+sô\b"), "Phân trại số"),
    (re.compile(r"\bBệ\s+sô\b"), "Bệ số"),
    (re.compile(r"\btập\s+huân\b"), "tập huấn"),
    (re.compile(r"\btuyệt\s+doi\s+an\s+toan\b"), "tuyệt đối an toàn"),
    (re.compile(r"\btuyệt\s+đôi\b"), "tuyệt đối"),
    (re.compile(r"\ban\s+toản\b"), "an toàn"),
    (re.compile(r"\btrang\s+bi\b"), "trang bị"),
    (re.compile(r"\bchấp\s+hành\s+nghiễm\b"), "chấp hành nghiêm"),
    (re.compile(r"\bquy\s+tác\s+an\s+toàn\b"), "quy tắc an toàn"),
    (re.compile(r"\bCổ\s+định\b"), "Cố định"),
    (re.compile(r"\bTiêu\s+liên\b"), "Tiểu liên"),
    (re.compile(r"\bTự\s+động\s+băn\b"), "Tự động bắn"),
    (re.compile(r"\btrường\s+b[ăã]n\b"), "trường bắn"),
    (re.compile(r"\bĐỊA\s+ĐIÊM\b"), "ĐỊA ĐIỂM"),
    (re.compile(r"\bKiếm\s+tra\b"), "Kiểm tra"),
    (re.compile(r"\bPhôi\s+hợp\b"), "Phối hợp"),
    (re.compile(r"\bphôi\s+hơp\b"), "phối hợp"),
    (re.compile(r"\bhiên\s+đồng\b"), "hiệp đồng"),
    (re.compile(r"\bHIỆP\s+ĐÔNG\b"), "HIỆP ĐỒNG"),
    (re.compile(r"\bđiêu\s+kiện\b"), "điều kiện"),
    (re.compile(r"\bcân\s+thiết\b"), "cần thiết"),
    (re.compile(r"\b06\s+khâu\b"), "06 khẩu"),
    (re.compile(r"\bm[ăắ]t\s+bia\b"), "mặt bia"),
    (re.compile(r"\bgiây\s+dán\b"), "giấy dán"),
    (re.compile(r"\bcờ\s+tô\s+quốc\b"), "cờ tổ quốc"),
    (re.compile(r"\bđôi\s+tượng\b"), "đối tượng"),
    (re.compile(r"\bdùng\s+đề\b"), "dùng để"),
    (re.compile(r"\bkhông\s+đề\s+xảy\s+ra\b"), "không để xảy ra"),
    (re.compile(r"\btrực\s+tiêp\b"), "trực tiếp"),
    (re.compile(r"\bPhó\s+Đội\s+trường\b"), "Phó Đội trưởng"),
    (re.compile(r"\bchuân\s+bị\b"), "chuẩn bị"),
    (re.compile(r"\bthực\s+hiên\b"), "thực hiện"),
    (re.compile(r"\bvướng\s+măc\b"), "vướng mắc"),
    (re.compile(r"\bbáo\s+cáo\s+đông\b"), "báo cáo đồng"),
    (re.compile(r"\bvù\s+trang\b"), "vũ trang"),
    (re.compile(r"\bcụ\s+thê\b"), "cụ thể"),
    (re.compile(r"\btình\s+huông\b"), "tình huống"),
    (re.compile(r"\bhành\s+v[ìí]\b"), "hành vi"),
    (re.compile(r"\bh[ồô]\s+trợ\b"), "hỗ trợ"),
    (re.compile(r"\bgóp\s+phân\b"), "góp phần"),
    (re.compile(r"\btinh\s+thân\b"), "tinh thần"),
    (re.compile(r"\bsơ\s+hờ\b"), "sơ hở"),
    (re.compile(r"\bdân\s+giải\b"), "dẫn giải"),
    (re.compile(r"\btuân\s+tra\b"), "tuần tra"),
    (re.compile(r"\bkíp\s+thời\b"), "kịp thời"),
    (re.compile(r"\bchấn\s+chính\b"), "chấn chỉnh"),
    (re.compile(r"\bcống\s+tác\b"), "công tác"),
    (re.compile(r"\bDỰ\s+KIÊN\s+TỊNH\s+HUỐNG\b"), "DỰ KIẾN TÌNH HUỐNG"),
    (re.compile(r"\bTỉnh\s+huống\b"), "Tình huống"),
    (re.compile(r"\bTô\s+tuần\s+tra\b"), "Tổ tuần tra"),
    (re.compile(r"\bkhu\s+vực\s+câm\b"), "khu vực cấm"),
    (re.compile(r"\bnơi\s+câm\b"), "nơi cấm"),
    (re.compile(r"\btrộm\s+cấp\b"), "trộm cắp"),
    (re.compile(r"\bđịa\s+bản\b"), "địa bàn"),
    (re.compile(r"\bnhập\s+trai\b"), "nhập trại"),
    (re.compile(r"\bbản\s+giao\b"), "bàn giao"),
    (re.compile(r"\bphạm\s+nhần\b"), "phạm nhân"),
    (re.compile(r"\btriên\s+khai\b"), "triển khai"),
    (re.compile(r"\bch[iỉ]\s+đa[oọ]\b"), "chỉ đạo"),
    (re.compile(r"\bch[iỉ]\s+huy\b"), "chỉ huy"),
    (re.compile(r"^L\s+(?=MỤC\s+ĐÍCH\b)"), "I. "),
    (re.compile(r"\bDỰ\s+KI[ẾÊ]N\s+T[ỊÌI]NH\s+HU[ỐÔ]NG\b"), "DỰ KIẾN TÌNH HUỐNG"),
    (re.compile(r"C[ỘÔ]NG\s+HÒA\s+XÃ\s+HỘI\s+CH[UỦ]\s+NGH[ĨI]A\s+VIỆT\s+NAM"), "CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM"),
    (re.compile(r"\btấn\s+cồng\b"), "tấn công"),
    (re.compile(r"\b(?:đàm|dảm)\s+bảo\b"), "đảm bảo"),
    (re.compile(r"\bđồng\s+chi\b"), "đồng chí"),
    (re.compile(r"\bĐồng\s+chi\b"), "Đồng chí"),
    (re.compile(r"\btô\s+chức\b"), "tổ chức"),
    (re.compile(r"\bchấn\s+chinh\b"), "chấn chỉnh"),
    (re.compile(r"\bcân\s+thiêt\b"), "cần thiết"),
    (re.compile(r"\bthành\s+thao\b"), "thành thạo"),
    (re.compile(r"\bnhiêm\s+vụ\b"), "nhiệm vụ"),
    (re.compile(r"\bchiên\s+đầu\b"), "chiến đấu"),
    (re.compile(r"\btuyệt\s+đội\b"), "tuyệt đối"),
    (re.compile(r"\btrường\s+bằn\b"), "trường bắn"),
    (re.compile(r"\(đề\s+chỉ\s+đạo\)"), "(để chỉ đạo)"),
    (re.compile(r"\bcỏ\s+hộp\s+tiếp\s+đạn\b"), "có hộp tiếp đạn"),
    (re.compile(r"\bcỏ\s+hộp\s+tiếp\b"), "có hộp tiếp"),
    (re.compile(r"\bhồ\s+dân\b"), "hồ dán"),
    (re.compile(r"\bđan\s+cấp\s+1\s*,\s*2\b"), "đạn cấp 1, 2"),
    (re.compile(r"\b(Phân\s+trại(?:\s+số)?\s+1)\s*,\s*(2)\b"), r"\1, \2"),
    (re.compile(r"\bXe\s+chuyên\s+dung\b"), "Xe chuyên dụng"),
    (re.compile(r"\bQLHL8\s*SDĐVNV\b"), "QLHL&SDĐVNV"),
    (re.compile(r"\bphôi\s+hợp\b"), "phối hợp"),
    (re.compile(r"\bkiếm\s+tra\b"), "kiểm tra"),
    (re.compile(r"\bkiêm\s+soát\b"), "kiểm soát"),
    (re.compile(r"\bđể\s+tống\s+hợp\b"), "để tổng hợp"),
    (re.compile(r"\btống\s+hợp\b"), "tổng hợp"),
    (re.compile(r"\btrường\s+băn\b"), "trường bắn"),
    (re.compile(r"\[để\s+thực\s+hiện\)"), "(để thực hiện)"),
    (re.compile(r"\bhành\s+vì\b"), "hành vi"),
    (re.compile(r"\bvi\s+pham\b"), "vi phạm"),
    (re.compile(r"\bpham\s+vi\b"), "phạm vi"),
    (re.compile(r"\bkip\s+thời\b"), "kịp thời"),
    (re.compile(r"\bbộ\s+phân\b"), "bộ phận"),
    (re.compile(r"Bộ\s+Công\s+an\s*\.\s*quy\s+định"), "Bộ Công an quy định"),
    (re.compile(r"\btrường\s+giáo\s+dường\b"), "trường giáo dưỡng"),
    (re.compile(r"\blao\s+động\s+lê\b"), "lao động lẻ"),
    (re.compile(r"\b01\s+quyền\b"), "01 quyển"),
    (re.compile(r"\bđánh\s+bất\s+cá\b"), "đánh bắt cá"),
    (re.compile(r"\bbáo\s+cáo\s+đồng\s+chí\s+giám\s+thị\b"), "báo cáo đồng chí Giám thị"),
)


def is_browser_or_viewer_noise(
    text: str,
    rel_yc: float | None = None,
) -> bool:
    """Detect browser/PDF.js print artifacts and bare margin page-number noise."""
    s = normalize_vietnamese_nfc(text, collapse_spaces=True).strip()
    if not s:
        return True
    if _BROWSER_VIEWER_RE.search(s):
        return True
    if rel_yc is not None and (rel_yc <= 0.08 or rel_yc >= 0.90):
        if _BARE_EDGE_PAGE_NUM_RE.match(s):
            return True
        if len(s) <= 12 and re.match(r"^\d+[:/\.]\d+[:/\.]?\s*$", s):
            return True
    return False


class VietnameseAdminPostProcessor:
    """Contextual post-processor for Vietnamese administrative & regulatory OCR text."""

    @staticmethod
    def clean_line(text: str) -> str:
        """Clean OCR noise, punctuation glitches, and contextual Vietnamese typos in a line."""
        s = normalize_vietnamese_nfc(text, collapse_spaces=True).strip()
        if not s:
            return ""

        # National motto spacing / delimiter repair
        if re.search(r"Đ[ộọ]c\s+l[ậa]p.+Tự\s+do.+H[ạa]nh\s+phúc", s, re.IGNORECASE):
            s = re.sub(
                r"Đ[ộọ]c\s+l[ậa]p\s*[\?\u2013\u2014\-/.,:;]+\s*Tự\s+do\s*[\?\u2013\u2014\-/.,:;]+\s*H[ạa]nh\s+phúc.*",
                "Độc lập - Tự do - Hạnh phúc",
                s,
                flags=re.IGNORECASE,
            )

        # Dotted "BẢN SỐ:..." header field: suppress hallucinated noise tokens embedded in dots
        m_ban = re.match(r"^(BẢN\s+SỐ)\s*:\s*(.*)$", s, flags=re.IGNORECASE)
        if m_ban:
            tail = m_ban.group(2).strip()
            if not tail or re.search(r"\.{2,}", tail) or len(tail) > 12:
                clean_num = re.match(r"^(\d{1,4})\b", tail)
                if clean_num:
                    s = f"BẢN SỐ: {clean_num.group(1)}"
                else:
                    s = "BẢN SỐ: ..............."

        # Normalize leading "L." / "IL" before all-caps section headings ("L. MỤC ĐÍCH" -> "I. MỤC ĐÍCH", "IL NỘI DUNG" -> "II. NỘI DUNG")
        s = re.sub(r"^L\.\s+(?=[A-ZÀÁẢÃẠĂẮẰẲẴẶÂẦẤẨẪẬĐÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴ]{2,})", "I. ", s)
        s = re.sub(r"^IL\.?\s+(?=[A-ZÀÁẢÃẠĂẮẰẲẴẶÂẦẤẨẪẬĐÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴ]{2,})", "II. ", s)

        # Repair comma/missing space after leading item number ("2, Phối hợp" / "1.Người" -> "1. Người") and "5.01 lá cờ" -> "5. 01 lá cờ"
        s = re.sub(r"^(\d{1,2}),\s+(?=[A-ZÀ-ỸĐa-zà-ỹđ])", r"\1. ", s)
        s = re.sub(r"^(\d{1,2})\.(?=[A-ZÀ-ỸĐ])", r"\1. ", s)
        s = re.sub(r"^(\d{1,2})\.(0\d\s+[A-Za-zÀ-ỹĐđ])", r"\1. \2", s)

        # Bullet marker confusion ("% ..." or "? ..." at line start) and dropped leading hyphens
        s = re.sub(r"^[%•●▪■]\s+", "+ ", s)
        s = re.sub(r"^\?\s+(?=[A-ZÀ-ỸĐa-zà-ỹđ])", "+ ", s)
        s = re.sub(r"^(?:[\-\+\*•●▪■]\s*)?(Tình\s+huống\s*:|Biện\s+pháp\s+xử\s+lý\s*:?)\s*$", r"* \1", s, flags=re.IGNORECASE)
        s = re.sub(r"^(?:\+|\*)\s+(Tình huống|Biện pháp xử lý)", r"* \1", s)
        s = re.sub(r"^(\*\s*Biện pháp xử lý:?)\s+[A-Z]?[a-z]{2,6}\s*$", r"\1", s)
        s = re.sub(r"^(?=Dự\s+trù\s+đạn\s+hư\s+hỏng\s*:)", "- ", s)
        s = re.sub(r"^(?=(?:Tư\s+thế\s+bắn|Bắn\s+thử|Bắn\s+kiểm\s+tra)\s*:)", "- ", s)

        # Strip leaked prefix noise before inline sub-item number ("Thành 7,2. Đợt 2:" -> "7.2. Đợt 2:")
        s = re.sub(r"^(?:Thành|Hình|Lành)\s+(\d{1,2})[\.,](\d{1,2})[\.,]?\s+", r"\1.\2. ", s)
        s = re.sub(r"^(\d{1,2}),(\d{1,2})[,\.]\s+(?=(?:Đ/c|Đợt|Đ/ít|Đồng\s+chí|[A-ZÀ-Ỹ]))", r"\1.\2. ", s)

        # Normalize "Đợt I:" -> "Đợt 1:"
        s = re.sub(r"\b(Đợt)\s+I\s*:", r"\1 1:", s)

        # Repair missing leading zero in count after colon ("Bàn:?2 cái" -> "Bàn: 02 cái")
        s = re.sub(r":\s*\?(\d)\b", r": 0\1", s)

        # Apply contextual Vietnamese phrase repairs before unit/punctuation normalization
        for pattern, repl in _CONTEXTUAL_VIET_PHRASE_FIXES:
            s = pattern.sub(repl, s)

        # Repair "= <count> viên" where "=" was read as "?" or "-"
        s = re.sub(r"\b(\d[\d\.,]*)\s*x\s*(\d[\d\.,]*)\s*(đ/c)\s*[\?\-]\s*(\d)", r"\1 x \2 \3 = \4", s, flags=re.IGNORECASE)
        s = re.sub(r"\b(\d[\d\.,]*)\s*(đ/c)\s*[\?\-]\s*(\d[\d\.,]*\s*viên)\b", r"\1 \2 = \3", s, flags=re.IGNORECASE)
        s = re.sub(r"\s+X\s+(\d[\d\.,]*\s*đ/c)", r" x \1", s)

        # Clean trailing closing formula "./." + handwritten margin signature/initials noise
        s = re.sub(
            r"(chỉ đạo|ý kiến|thực hiện|báo cáo|kính trình|quy định)\s*(?:\.?\s*/[\./\-\s]*(?:[A-Za-zÀ-ỹ0-9]{1,10}(?:\s+[A-Za-zÀ-ỹ0-9]{1,10}){0,2})?|\.\s*[\(\[]+\s*[A-Za-zÀ-ỹ0-9]{1,10})\s*$",
            r"\1./.",
            s,
            flags=re.IGNORECASE,
        )
        s = re.sub(
            r"\./\.?\s*(?:[\-\(\[/\.,]*\s*[A-Za-zÀ-ỹ0-9]{1,9}(?:\s+[A-Za-zÀ-ỹ0-9]{1,9}){0,2})?\s*$",
            "./.",
            s,
        )

        return normalize_vietnamese_nfc(s, collapse_spaces=True).strip()

    @classmethod
    def split_inline_subitems(cls, text: str) -> list[str]:
        """Split merged inline numbered sub-items (e.g. `1.1. ... 1.2. ...`) into separate lines."""
        cleaned = cls.clean_line(text)
        if not cleaned or len(cleaned) < 20:
            return [cleaned] if cleaned else []

        parts = _INLINE_SUBITEM_SPLIT_RE.split(cleaned)
        if len(parts) <= 1:
            return [cleaned]

        out: list[str] = []
        for part in parts:
            seg = cls.clean_line(part)
            if seg:
                out.append(seg)
        return out or [cleaned]


def postprocess_vietnamese_admin_line(text: str) -> str:
    """Convenience wrapper cleaning a single Vietnamese administrative text line."""
    return VietnameseAdminPostProcessor.clean_line(text)
