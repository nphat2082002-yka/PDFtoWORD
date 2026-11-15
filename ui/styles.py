"""Office 365 / Fluent Design palette and stylesheet constants for PDFtoWord GUI."""

from __future__ import annotations

import sys
from pathlib import Path

APP_VERSION = "1.0.0"
AUTHOR_NAME = "Huỳnh Ngọc Phát"
COPYRIGHT_TEXT = "© 2026 Bản quyền bởi Huỳnh Ngọc Phát"


def _resolve_assets_dir() -> Path:
    candidates = [
        Path(__file__).resolve().parent / "assets",
        Path(sys.executable).resolve().parent / "ui" / "assets",
        Path(sys.executable).resolve().parent / "app_src" / "ui" / "assets",
    ]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "ui" / "assets")
    for cand in candidates:
        if cand.is_dir():
            return cand
    return candidates[0]


ASSETS_DIR = _resolve_assets_dir()
APP_ICON_PNG = ASSETS_DIR / "app_icon.png"
APP_ICON_ICO = ASSETS_DIR / "app_icon.ico"


def load_app_icon():
    """Load the shared application QIcon from `.ico` (or `.png` fallback)."""
    from PySide6.QtGui import QIcon

    for cand in (APP_ICON_ICO, APP_ICON_PNG):
        if cand.is_file():
            icon = QIcon(str(cand))
            if not icon.isNull():
                return icon
    return QIcon()


WORD_BLUE = "#185ABD"
WORD_DARK = "#0F3D82"
WORD_RIBBON = "#2B579A"
WORD_HOVER = "#114899"
WORD_LIGHT = "#EBF3FC"

PDF_RED = "#E53935"
PDF_LIGHT = "#FDECEC"

EXCEL_GREEN = "#107C41"
EXCEL_HOVER = "#0B5E31"
EXCEL_LIGHT = "#EAF6EF"

BG_LIGHT = "#F3F4F6"
CARD_WHITE = "#FFFFFF"
BORDER_GRAY = "#E5E7EB"
TEXT_PRIMARY = "#1F2937"
TEXT_SECONDARY = "#4B5563"
TEXT_MUTED = "#6B7280"

MAIN_STYLESHEET = f"""
QMainWindow, QWidget#CentralWorkspace {{
    background-color: {BG_LIGHT};
    font-family: "Segoe UI", "Arial", sans-serif;
    color: {TEXT_PRIMARY};
    font-size: 10pt;
}}

QFrame#RibbonBar {{
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 {WORD_DARK}, stop: 0.45 {WORD_BLUE}, stop: 1 {WORD_RIBBON}
    );
    border-bottom: 1px solid {WORD_DARK};
}}

QLabel#RibbonTitle {{
    color: #FFFFFF;
    font-size: 13pt;
    font-weight: 700;
}}

QLabel#RibbonSubtitle {{
    color: #DBEAFE;
    font-size: 8.5pt;
}}

QPushButton#RibbonBtn {{
    background-color: rgba(255, 255, 255, 0.14);
    color: #FFFFFF;
    border: 1px solid rgba(255, 255, 255, 0.28);
    border-radius: 6px;
    padding: 6px 13px;
    font-size: 9.5pt;
    font-weight: 600;
}}

QPushButton#RibbonBtn:hover {{
    background-color: rgba(255, 255, 255, 0.26);
    border-color: rgba(255, 255, 255, 0.50);
}}

QPushButton#RibbonBtn:pressed {{
    background-color: rgba(255, 255, 255, 0.10);
}}

QPushButton#RibbonBtn:disabled {{
    background-color: rgba(255, 255, 255, 0.06);
    color: rgba(255, 255, 255, 0.45);
    border-color: rgba(255, 255, 255, 0.14);
}}

QLabel#EngineBadge {{
    background-color: #DCFCE7;
    color: #166534;
    border: 1px solid #86EFAC;
    border-radius: 12px;
    padding: 4px 12px;
    font-size: 9pt;
    font-weight: 600;
}}

QLabel#EngineBadge[ready="false"] {{
    background-color: #FEF3C7;
    color: #92400E;
    border: 1px solid #FCD34D;
}}

QFrame#OfficeCard {{
    background-color: {CARD_WHITE};
    border: 1px solid {BORDER_GRAY};
    border-radius: 10px;
}}

QLabel#SectionTitle {{
    color: {TEXT_PRIMARY};
    font-size: 11.5pt;
    font-weight: 700;
}}

QLabel#FieldLabel {{
    color: {TEXT_SECONDARY};
    font-size: 9.5pt;
    font-weight: 600;
}}

QLabel#MutedLabel {{
    color: {TEXT_MUTED};
    font-size: 9pt;
}}

QFrame#DropZone {{
    background-color: #F8FAFC;
    border: 2px dashed #94A3B8;
    border-radius: 12px;
}}

QFrame#DropZone:hover, QFrame#DropZone[dragOver="true"] {{
    background-color: {WORD_LIGHT};
    border: 2px dashed {WORD_BLUE};
}}

QLabel#PreviewFrame {{
    background-color: #F1F5F9;
    border: 1px solid #CBD5E1;
    border-radius: 8px;
}}

QFrame#InfoBanner {{
    background-color: {WORD_LIGHT};
    border: 1px solid #BFDBFE;
    border-radius: 8px;
}}

QFrame#SuccessBanner {{
    background-color: {EXCEL_LIGHT};
    border: 1px solid #86EFAC;
    border-radius: 10px;
}}

QLineEdit#OfficeInput {{
    background-color: #F9FAFB;
    border: 1px solid #D1D5DB;
    border-radius: 6px;
    padding: 7px 10px;
    color: {TEXT_PRIMARY};
    font-size: 9.5pt;
}}

QLineEdit#OfficeInput:focus {{
    background-color: #FFFFFF;
    border-color: {WORD_BLUE};
}}

QPushButton#PrimaryBtn {{
    background-color: {WORD_BLUE};
    color: #FFFFFF;
    border: none;
    border-radius: 8px;
    padding: 12px 20px;
    font-size: 11pt;
    font-weight: 700;
}}

QPushButton#PrimaryBtn:hover {{
    background-color: {WORD_HOVER};
}}

QPushButton#PrimaryBtn:pressed {{
    background-color: {WORD_DARK};
}}

QPushButton#PrimaryBtn:disabled {{
    background-color: #93C5FD;
    color: #EFF6FF;
}}

QPushButton#SuccessBtn {{
    background-color: {EXCEL_GREEN};
    color: #FFFFFF;
    border: none;
    border-radius: 8px;
    padding: 11px 18px;
    font-size: 10.5pt;
    font-weight: 700;
}}

QPushButton#SuccessBtn:hover {{
    background-color: {EXCEL_HOVER};
}}

QPushButton#SecondaryBtn {{
    background-color: #FFFFFF;
    color: {TEXT_PRIMARY};
    border: 1px solid #D1D5DB;
    border-radius: 7px;
    padding: 8px 14px;
    font-size: 9.5pt;
    font-weight: 600;
}}

QPushButton#SecondaryBtn:hover {{
    background-color: #F9FAFB;
    border-color: {WORD_BLUE};
    color: {WORD_BLUE};
}}

QPushButton#SecondaryBtn:disabled {{
    color: #9CA3AF;
    border-color: #E5E7EB;
}}

QPushButton#DangerOutlineBtn {{
    background-color: #FFFFFF;
    color: #B91C1C;
    border: 1px solid #FCA5A5;
    border-radius: 7px;
    padding: 8px 16px;
    font-size: 9.5pt;
    font-weight: 600;
}}

QPushButton#DangerOutlineBtn:hover {{
    background-color: #FEF2F2;
    border-color: #EF4444;
}}

QProgressBar#OfficeProgress {{
    background-color: #E5E7EB;
    border: none;
    border-radius: 6px;
    height: 12px;
    text-align: center;
}}

QProgressBar#OfficeProgress::chunk {{
    border-radius: 6px;
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 {WORD_BLUE}, stop: 1 #2563EB
    );
}}

QPlainTextEdit#ActivityLog {{
    background-color: #F8FAFC;
    border: 1px solid {BORDER_GRAY};
    border-radius: 8px;
    padding: 8px;
    color: {TEXT_PRIMARY};
    font-family: "Consolas", "Segoe UI", monospace;
    font-size: 9pt;
}}

QFrame#FooterBar {{
    background-color: #FFFFFF;
    border-top: 1px solid {BORDER_GRAY};
}}

QLabel#FooterMeta {{
    color: {TEXT_MUTED};
    font-size: 8.5pt;
}}

QLabel#CopyrightLabel {{
    color: {WORD_DARK};
    font-size: 8.5pt;
    font-weight: 700;
}}
"""
