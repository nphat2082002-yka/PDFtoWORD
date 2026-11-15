"""Modern Microsoft Office 365 / Fluent Design MainWindow for PDFtoWord."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pymupdf as fitz
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QImage, QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from pdf2word import ConversionResult

from .styles import (
    APP_ICON_PNG,
    APP_VERSION,
    AUTHOR_NAME,
    COPYRIGHT_TEXT,
    MAIN_STYLESHEET,
    PDF_RED,
    WORD_BLUE,
    load_app_icon,
)
from .workers import ConversionWorker, EngineSingletonManager


def _format_file_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.2f} MB"


class PdfDropZone(QFrame):
    """Fluent drag-and-drop card for selecting or dropping an input PDF file."""

    file_dropped = Signal(str)
    browse_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setProperty("dragOver", False)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 32, 24, 32)

        badge = QLabel("PDF")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(72, 86)
        badge.setStyleSheet(
            f"background-color: {PDF_RED}; color: #FFFFFF; border-radius: 12px; "
            "font-size: 17pt; font-weight: 800; letter-spacing: 1px;"
        )
        layout.addWidget(badge, alignment=Qt.AlignmentFlag.AlignCenter)

        title = QLabel("Kéo thả file PDF vào đây")
        title.setStyleSheet("font-size: 13pt; font-weight: 700; color: #1F2937;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        desc = QLabel(
            "Hỗ trợ tài liệu hành chính Tiếng Việt (PDF Scan & PDF văn bản gốc).\n"
            "Tự động tách con dấu đỏ, giữ bảng biểu, cột & Floating TextBox."
        )
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc.setObjectName("MutedLabel")
        layout.addWidget(desc)

        self.btn_browse = QPushButton("Chọn file PDF từ máy tính...")
        self.btn_browse.setObjectName("PrimaryBtn")
        self.btn_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_browse.clicked.connect(self.browse_requested.emit)
        layout.addWidget(self.btn_browse, alignment=Qt.AlignmentFlag.AlignCenter)

    def _set_drag_over(self, active: bool) -> None:
        self.setProperty("dragOver", active)
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        mime = event.mimeData()
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf"):
                    self._set_drag_over(True)
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._set_drag_over(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        self._set_drag_over(False)
        for url in event.mimeData().urls():
            if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf"):
                self.file_dropped.emit(url.toLocalFile())
                event.acceptProposedAction()
                return
        event.ignore()


class StepIndicatorWidget(QWidget):
    """5-stage vertical pipeline progress stepper mirroring core conversion stages."""

    STAGES: tuple[tuple[str, str], ...] = (
        ("preprocess", "1. Phân tích hình học & Tách con dấu đỏ"),
        ("detect", "2. Phát hiện khung chữ (PaddleOCR)"),
        ("recognize", "3. Nhận diện Tiếng Việt (VietOCR)"),
        ("layout", "4. Tái tạo cấu trúc đoạn, bảng & ô Text trôi nổi"),
        ("export", "5. Chuẩn hóa thể thức & Đóng gói OpenXML (.docx)"),
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[tuple[QFrame, QLabel, QLabel]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(6)

        for _key, title in self.STAGES:
            row_frame = QFrame()
            row_frame.setStyleSheet(
                "background-color: #F8FAFC; border: 1px solid #E5E7EB; border-radius: 7px;"
            )
            row_layout = QHBoxLayout(row_frame)
            row_layout.setContentsMargins(10, 6, 10, 6)
            row_layout.setSpacing(8)

            icon_lbl = QLabel("○")
            icon_lbl.setFixedWidth(18)
            icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            icon_lbl.setStyleSheet("color: #9CA3AF; font-weight: 700; border: none; background: transparent;")

            text_lbl = QLabel(title)
            text_lbl.setStyleSheet("color: #6B7280; font-size: 9.5pt; border: none; background: transparent;")

            row_layout.addWidget(icon_lbl)
            row_layout.addWidget(text_lbl, stretch=1)
            layout.addWidget(row_frame)
            self._rows.append((row_frame, icon_lbl, text_lbl))

    def _apply_row_style(self, idx: int, state: str) -> None:
        frame, icon_lbl, text_lbl = self._rows[idx]
        if state == "done":
            frame.setStyleSheet(
                "background-color: #EAF6EF; border: 1px solid #86EFAC; border-radius: 7px;"
            )
            icon_lbl.setText("✓")
            icon_lbl.setStyleSheet("color: #107C41; font-weight: 800; border: none; background: transparent;")
            text_lbl.setStyleSheet(
                "color: #065F46; font-size: 9.5pt; font-weight: 600; border: none; background: transparent;"
            )
        elif state == "active":
            frame.setStyleSheet(
                "background-color: #EBF3FC; border: 1px solid #93C5FD; border-radius: 7px;"
            )
            icon_lbl.setText("●")
            icon_lbl.setStyleSheet(f"color: {WORD_BLUE}; font-weight: 800; border: none; background: transparent;")
            text_lbl.setStyleSheet(
                f"color: {WORD_BLUE}; font-size: 9.5pt; font-weight: 700; border: none; background: transparent;"
            )
        else:
            frame.setStyleSheet(
                "background-color: #F8FAFC; border: 1px solid #E5E7EB; border-radius: 7px;"
            )
            icon_lbl.setText("○")
            icon_lbl.setStyleSheet("color: #9CA3AF; font-weight: 700; border: none; background: transparent;")
            text_lbl.setStyleSheet("color: #6B7280; font-size: 9.5pt; border: none; background: transparent;")

    def reset(self) -> None:
        for i in range(len(self._rows)):
            self._apply_row_style(i, "pending")

    def set_active_step(self, step_key: str) -> None:
        stage_keys = [k for k, _ in self.STAGES]
        active_idx = stage_keys.index(step_key) if step_key in stage_keys else 0
        for idx in range(len(self._rows)):
            if idx < active_idx:
                self._apply_row_style(idx, "done")
            elif idx == active_idx:
                self._apply_row_style(idx, "active")
            else:
                self._apply_row_style(idx, "pending")

    def mark_all_done(self) -> None:
        for idx in range(len(self._rows)):
            self._apply_row_style(idx, "done")


class MainWindow(QMainWindow):
    """Main Office 365 Desktop Window with PDF preview, live worker progress & quick actions."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"VN-Office PDF to Word Pro v{APP_VERSION} — Bản quyền bởi {AUTHOR_NAME}")
        self.resize(1220, 780)
        self.setMinimumSize(1020, 680)
        self.setAcceptDrops(True)
        self.setStyleSheet(MAIN_STYLESHEET)
        icon = load_app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)

        self._input_pdf_path: Path | None = None
        self._fitz_doc: fitz.Document | None = None
        self._current_page_idx: int = 0
        self._total_pages: int = 0
        self._raw_preview_pixmap: QPixmap | None = None
        self._last_auto_output: str = ""
        self._last_result: ConversionResult | None = None
        self._last_output_docx: Path | None = None
        self._last_log_msg: str = ""
        self._start_ts: float = 0.0
        self._worker: ConversionWorker | None = None

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(100)
        self._elapsed_timer.timeout.connect(self._update_elapsed_label)

        self._build_ui()
        self.update_engine_status(
            EngineSingletonManager.is_ready,
            EngineSingletonManager.device_label,
        )
        self._refresh_action_states()

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("CentralWorkspace")
        self.setCentralWidget(central)

        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # 1. Top Office Ribbon Bar
        ribbon = QFrame()
        ribbon.setObjectName("RibbonBar")
        ribbon.setFixedHeight(70)
        ribbon_layout = QHBoxLayout(ribbon)
        ribbon_layout.setContentsMargins(20, 10, 20, 10)
        ribbon_layout.setSpacing(12)

        brand_badge = QLabel()
        brand_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_badge.setFixedSize(44, 44)
        brand_pixmap = QPixmap(str(APP_ICON_PNG)) if APP_ICON_PNG.is_file() else QPixmap()
        if not brand_pixmap.isNull():
            brand_badge.setPixmap(
                brand_pixmap.scaled(
                    42,
                    42,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            brand_badge.setStyleSheet("background: transparent;")
        else:
            brand_badge.setText("W")
            brand_badge.setStyleSheet(
                "background-color: #FFFFFF; color: #185ABD; border-radius: 8px; "
                "font-size: 16pt; font-weight: 800;"
            )
        ribbon_layout.addWidget(brand_badge)

        title_col = QVBoxLayout()
        title_col.setSpacing(1)
        lbl_title = QLabel("VN-Office PDF to Word Pro")
        lbl_title.setObjectName("RibbonTitle")
        lbl_sub = QLabel("AI ADRT Layout Engine • Chuyên văn bản hành chính Tiếng Việt")
        lbl_sub.setObjectName("RibbonSubtitle")
        title_col.addWidget(lbl_title)
        title_col.addWidget(lbl_sub)
        ribbon_layout.addLayout(title_col)

        ribbon_layout.addSpacerItem(
            QSpacerItem(20, 10, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        )

        self.lbl_engine_badge = QLabel("● AI Engine: Đang khởi tạo...")
        self.lbl_engine_badge.setObjectName("EngineBadge")
        self.lbl_engine_badge.setProperty("ready", False)
        ribbon_layout.addWidget(self.lbl_engine_badge)

        root_layout.addWidget(ribbon)

        # 2. Main 2-Column Split Workspace (40% Left / 60% Right)
        workspace = QHBoxLayout()
        workspace.setContentsMargins(18, 16, 18, 16)
        workspace.setSpacing(16)

        # LEFT CARD (40%)
        left_card = QFrame()
        left_card.setObjectName("OfficeCard")
        left_card_layout = QVBoxLayout(left_card)
        left_card_layout.setContentsMargins(16, 16, 16, 16)
        left_card_layout.setSpacing(10)

        left_hdr = QHBoxLayout()
        lbl_left_title = QLabel("Tài liệu PDF nguồn & Xem trước")
        lbl_left_title.setObjectName("SectionTitle")
        left_hdr.addWidget(lbl_left_title)
        left_hdr.addStretch()

        self.btn_change_pdf = QPushButton("Đổi file PDF")
        self.btn_change_pdf.setObjectName("SecondaryBtn")
        self.btn_change_pdf.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_change_pdf.clicked.connect(self.browse_pdf)
        left_hdr.addWidget(self.btn_change_pdf)
        left_card_layout.addLayout(left_hdr)

        self.left_stack = QStackedWidget()
        self.drop_zone = PdfDropZone()
        self.drop_zone.file_dropped.connect(self.load_pdf)
        self.drop_zone.browse_requested.connect(self.browse_pdf)
        self.left_stack.addWidget(self.drop_zone)

        preview_container = QWidget()
        preview_layout = QVBoxLayout(preview_container)
        preview_layout.setContentsMargins(0, 4, 0, 0)
        preview_layout.setSpacing(10)

        info_box = QFrame()
        info_box.setObjectName("InfoBanner")
        info_layout = QVBoxLayout(info_box)
        info_layout.setContentsMargins(12, 8, 12, 8)
        info_layout.setSpacing(2)
        self.lbl_pdf_name = QLabel("-")
        self.lbl_pdf_name.setStyleSheet("font-weight: 700; color: #1E3A8A; font-size: 10pt;")
        self.lbl_pdf_meta = QLabel("-")
        self.lbl_pdf_meta.setObjectName("MutedLabel")
        info_layout.addWidget(self.lbl_pdf_name)
        info_layout.addWidget(self.lbl_pdf_meta)
        preview_layout.addWidget(info_box)

        self.lbl_preview_image = QLabel("Đang dựng ảnh xem trước...")
        self.lbl_preview_image.setObjectName("PreviewFrame")
        self.lbl_preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_preview_image.setMinimumHeight(380)
        self.lbl_preview_image.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        preview_layout.addWidget(self.lbl_preview_image, stretch=1)

        nav_row = QHBoxLayout()
        nav_row.setSpacing(10)
        self.btn_prev_page = QPushButton("< Trang trước")
        self.btn_prev_page.setObjectName("SecondaryBtn")
        self.btn_prev_page.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_prev_page.clicked.connect(self.prev_page)

        self.lbl_page_nav = QLabel("Trang 0 / 0")
        self.lbl_page_nav.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_page_nav.setStyleSheet("font-weight: 700; color: #1F2937;")

        self.btn_next_page = QPushButton("Trang sau >")
        self.btn_next_page.setObjectName("SecondaryBtn")
        self.btn_next_page.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_next_page.clicked.connect(self.next_page)

        nav_row.addWidget(self.btn_prev_page)
        nav_row.addWidget(self.lbl_page_nav, stretch=1)
        nav_row.addWidget(self.btn_next_page)
        preview_layout.addLayout(nav_row)

        self.left_stack.addWidget(preview_container)
        left_card_layout.addWidget(self.left_stack, stretch=1)

        workspace.addWidget(left_card, stretch=4)

        # RIGHT COLUMN (60%)
        right_col = QVBoxLayout()
        right_col.setSpacing(14)

        # Dynamic State Card (Ready / Processing / Completed)
        state_card = QFrame()
        state_card.setObjectName("OfficeCard")
        state_card_layout = QVBoxLayout(state_card)
        state_card_layout.setContentsMargins(16, 16, 16, 16)

        self.right_stack = QStackedWidget()

        # State 0: Ready
        ready_view = QWidget()
        ready_layout = QVBoxLayout(ready_view)
        ready_layout.setContentsMargins(0, 0, 0, 0)
        ready_layout.setSpacing(14)

        lbl_ready_hdr = QLabel("Cấu hình Chuyển đổi Tối ưu (Editable Copy)")
        lbl_ready_hdr.setObjectName("SectionTitle")
        ready_layout.addWidget(lbl_ready_hdr)

        cfg_box = QFrame()
        cfg_box.setObjectName("InfoBanner")
        cfg_layout = QVBoxLayout(cfg_box)
        cfg_layout.setContentsMargins(14, 12, 14, 12)
        cfg_layout.setSpacing(6)
        cfg_items = (
            "• Ngôn ngữ & Nhận diện: Tiếng Việt chuẩn Unicode NFC (PaddleOCR + VietOCR Transformer)",
            "• Độ phân giải xử lý ảnh: 300 DPI + Khử nghiêng & Tách dấu đỏ tự động",
            "• Tái tạo cấu trúc Word: Giữ bảng biểu, cột đôi ẩn viền, Floating TextBox (MẬT/BẢN SỐ)",
            "• Kiểu chữ hành chính: Times New Roman chuẩn Nghị định 30 (Quốc hiệu, Heading, Nơi nhận)",
        )
        for line in cfg_items:
            lbl = QLabel(line)
            lbl.setWordWrap(True)
            lbl.setStyleSheet("color: #1E3A8A; font-size: 9.5pt;")
            cfg_layout.addWidget(lbl)
        ready_layout.addWidget(cfg_box)

        out_row = QHBoxLayout()
        lbl_out = QLabel("Lưu thành (.docx):")
        lbl_out.setObjectName("FieldLabel")
        self.txt_output_path = QLineEdit()
        self.txt_output_path.setObjectName("OfficeInput")
        self.txt_output_path.setPlaceholderText("Đường dẫn file .docx đầu ra...")
        self.btn_pick_output = QPushButton("Chọn nơi lưu...")
        self.btn_pick_output.setObjectName("SecondaryBtn")
        self.btn_pick_output.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pick_output.clicked.connect(self.choose_output_path)
        out_row.addWidget(lbl_out)
        out_row.addWidget(self.txt_output_path, stretch=1)
        out_row.addWidget(self.btn_pick_output)
        ready_layout.addLayout(out_row)

        self.lbl_ready_hint = QLabel("Chọn file PDF ở cột bên trái để bắt đầu chuyển đổi sang Word.")
        self.lbl_ready_hint.setObjectName("MutedLabel")
        ready_layout.addWidget(self.lbl_ready_hint)

        ready_layout.addStretch()

        self.btn_start_convert = QPushButton("BẮT ĐẦU CHUYỂN ĐỔI SANG WORD (.DOCX)")
        self.btn_start_convert.setObjectName("PrimaryBtn")
        self.btn_start_convert.setMinimumHeight(50)
        self.btn_start_convert.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_start_convert.clicked.connect(self.start_conversion)
        ready_layout.addWidget(self.btn_start_convert)

        self.right_stack.addWidget(ready_view)

        # State 1: Processing
        proc_view = QWidget()
        proc_layout = QVBoxLayout(proc_view)
        proc_layout.setContentsMargins(0, 0, 0, 0)
        proc_layout.setSpacing(10)

        proc_top_row = QHBoxLayout()
        self.lbl_proc_pct = QLabel("0%")
        self.lbl_proc_pct.setStyleSheet(f"font-size: 20pt; font-weight: 800; color: {WORD_BLUE};")
        self.lbl_elapsed_time = QLabel("Thời gian đã chạy: 00:00.0s")
        self.lbl_elapsed_time.setObjectName("FieldLabel")
        proc_top_row.addWidget(self.lbl_proc_pct)
        proc_top_row.addStretch()
        proc_top_row.addWidget(self.lbl_elapsed_time)
        proc_layout.addLayout(proc_top_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("OfficeProgress")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        proc_layout.addWidget(self.progress_bar)

        self.lbl_proc_status = QLabel("Đang chuẩn bị tiến trình chuyển đổi...")
        self.lbl_proc_status.setWordWrap(True)
        self.lbl_proc_status.setStyleSheet("font-weight: 600; color: #1F2937; font-size: 10pt;")
        proc_layout.addWidget(self.lbl_proc_status)

        self.step_indicator = StepIndicatorWidget()
        proc_layout.addWidget(self.step_indicator)

        lbl_log_hdr = QLabel("Nhật ký xử lý trực tiếp:")
        lbl_log_hdr.setObjectName("FieldLabel")
        proc_layout.addWidget(lbl_log_hdr)

        self.txt_activity_log = QPlainTextEdit()
        self.txt_activity_log.setObjectName("ActivityLog")
        self.txt_activity_log.setReadOnly(True)
        self.txt_activity_log.setMaximumBlockCount(400)
        proc_layout.addWidget(self.txt_activity_log, stretch=1)

        cancel_row = QHBoxLayout()
        cancel_row.addStretch()
        self.btn_cancel = QPushButton("Hủy xử lý (Cancel)")
        self.btn_cancel.setObjectName("DangerOutlineBtn")
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.clicked.connect(self.cancel_conversion)
        cancel_row.addWidget(self.btn_cancel)
        proc_layout.addLayout(cancel_row)

        self.right_stack.addWidget(proc_view)

        # State 2: Completed
        done_view = QWidget()
        done_layout = QVBoxLayout(done_view)
        done_layout.setContentsMargins(0, 0, 0, 0)
        done_layout.setSpacing(14)

        success_banner = QFrame()
        success_banner.setObjectName("SuccessBanner")
        sb_layout = QVBoxLayout(success_banner)
        sb_layout.setContentsMargins(16, 14, 16, 14)
        sb_layout.setSpacing(6)

        lbl_done_title = QLabel("✓ Chuyển đổi PDF sang Word hoàn tất!")
        lbl_done_title.setStyleSheet("color: #107C41; font-size: 13pt; font-weight: 800;")
        sb_layout.addWidget(lbl_done_title)

        self.lbl_done_summary = QLabel("-")
        self.lbl_done_summary.setWordWrap(True)
        self.lbl_done_summary.setStyleSheet("color: #065F46; font-size: 10pt;")
        sb_layout.addWidget(self.lbl_done_summary)
        done_layout.addWidget(success_banner)

        lbl_next_hint = QLabel("Chọn thao tác tiếp theo với tài liệu Word vừa tạo:")
        lbl_next_hint.setObjectName("FieldLabel")
        done_layout.addWidget(lbl_next_hint)

        btn_grid_top = QHBoxLayout()
        btn_grid_top.setSpacing(10)
        self.btn_open_word = QPushButton("Mở file Word vừa tạo")
        self.btn_open_word.setObjectName("SuccessBtn")
        self.btn_open_word.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open_word.clicked.connect(self.open_output_docx)

        self.btn_save_as = QPushButton("Lưu file Word như... (Save As)")
        self.btn_save_as.setObjectName("PrimaryBtn")
        self.btn_save_as.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_save_as.clicked.connect(lambda: self.save_output_as())

        btn_grid_top.addWidget(self.btn_open_word, stretch=1)
        btn_grid_top.addWidget(self.btn_save_as, stretch=1)
        done_layout.addLayout(btn_grid_top)

        btn_grid_bot = QHBoxLayout()
        btn_grid_bot.setSpacing(10)
        self.btn_open_folder = QPushButton("Mở thư mục chứa file")
        self.btn_open_folder.setObjectName("SecondaryBtn")
        self.btn_open_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open_folder.clicked.connect(self.open_output_folder)

        self.btn_new_file = QPushButton("Chọn file PDF mới")
        self.btn_new_file.setObjectName("SecondaryBtn")
        self.btn_new_file.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_new_file.clicked.connect(lambda: self.reset_for_new_file(open_dialog=True))

        btn_grid_bot.addWidget(self.btn_open_folder, stretch=1)
        btn_grid_bot.addWidget(self.btn_new_file, stretch=1)
        done_layout.addLayout(btn_grid_bot)

        done_layout.addStretch()

        self.right_stack.addWidget(done_view)

        state_card_layout.addWidget(self.right_stack)
        right_col.addWidget(state_card, stretch=1)

        workspace.addLayout(right_col, stretch=6)
        root_layout.addLayout(workspace, stretch=1)

        # 3. Bottom Copyright Footer Bar
        footer_bar = QFrame()
        footer_bar.setObjectName("FooterBar")
        footer_bar.setFixedHeight(32)
        footer_layout = QHBoxLayout(footer_bar)
        footer_layout.setContentsMargins(20, 0, 20, 0)
        footer_layout.setSpacing(12)

        self.lbl_footer_meta = QLabel(
            f"VN-Office PDF to Word Pro v{APP_VERSION} • AI ADRT Layout Engine (PaddleOCR + VietOCR)"
        )
        self.lbl_footer_meta.setObjectName("FooterMeta")
        footer_layout.addWidget(self.lbl_footer_meta)

        footer_layout.addStretch()

        self.lbl_copyright = QLabel(COPYRIGHT_TEXT)
        self.lbl_copyright.setObjectName("CopyrightLabel")
        footer_layout.addWidget(self.lbl_copyright)

        root_layout.addWidget(footer_bar)

    def update_engine_status(self, is_ready: bool, device_label: str) -> None:
        """Update top-right Office Ribbon engine readiness pill."""
        if is_ready:
            self.lbl_engine_badge.setText(
                f"● AI Engine: Sẵn sàng (PaddleOCR + VietOCR • {device_label})"
            )
            self.lbl_engine_badge.setProperty("ready", True)
        else:
            self.lbl_engine_badge.setText(f"● AI Engine: Chế độ chờ ({device_label})")
            self.lbl_engine_badge.setProperty("ready", False)
        self.lbl_engine_badge.style().unpolish(self.lbl_engine_badge)
        self.lbl_engine_badge.style().polish(self.lbl_engine_badge)

    def _is_processing(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _refresh_action_states(self) -> None:
        processing = self._is_processing()
        has_pdf = self._input_pdf_path is not None and self._input_pdf_path.is_file()
        in_completed_view = self.right_stack.currentIndex() == 2

        can_change_pdf = has_pdf and not processing and not in_completed_view
        self.btn_change_pdf.setVisible(can_change_pdf)
        self.btn_change_pdf.setEnabled(can_change_pdf)

        self.btn_pick_output.setEnabled(not processing)
        self.txt_output_path.setEnabled(not processing)
        self.btn_start_convert.setEnabled(has_pdf and not processing)

        if self._total_pages > 0:
            self.btn_prev_page.setEnabled((not processing) and self._current_page_idx > 0)
            self.btn_next_page.setEnabled(
                (not processing) and self._current_page_idx < (self._total_pages - 1)
            )
        else:
            self.btn_prev_page.setEnabled(False)
            self.btn_next_page.setEnabled(False)

    def _close_pdf_doc(self) -> None:
        if self._fitz_doc is not None:
            try:
                self._fitz_doc.close()
            except Exception:
                pass
            self._fitz_doc = None
        self._raw_preview_pixmap = None

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if not self._is_processing() and event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf"):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        if self._is_processing():
            event.ignore()
            return
        for url in event.mimeData().urls():
            if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf"):
                self.load_pdf(url.toLocalFile())
                event.acceptProposedAction()
                return
        event.ignore()

    def browse_pdf(self) -> None:
        """Open Windows file picker to select an input PDF document."""
        if self._is_processing():
            return
        start_dir = str(self._input_pdf_path.parent) if self._input_pdf_path else str(Path.cwd())
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "Chọn tài liệu PDF cần chuyển đổi",
            start_dir,
            "PDF Documents (*.pdf);;All Files (*.*)",
        )
        if chosen:
            self.load_pdf(chosen)

    def choose_output_path(self) -> None:
        """Select destination `.docx` path prior to running conversion."""
        if self._is_processing():
            return
        default_dir = self.txt_output_path.text().strip() or (
            str(self._input_pdf_path.with_name(f"{self._input_pdf_path.stem}_converted.docx"))
            if self._input_pdf_path
            else str(Path.cwd() / "output_converted.docx")
        )
        chosen, _ = QFileDialog.getSaveFileName(
            self,
            "Chọn vị trí lưu file Word (.docx)",
            default_dir,
            "Word Document (*.docx)",
        )
        if chosen:
            if not chosen.lower().endswith(".docx"):
                chosen = f"{chosen}.docx"
            self.txt_output_path.setText(chosen)

    def load_pdf(self, pdf_path: str | Path) -> bool:
        """Load an input PDF, populate metadata, suggest output path, and render page 1 preview."""
        if self._is_processing():
            return False

        target = Path(pdf_path).resolve()
        if not target.is_file():
            QMessageBox.warning(self, "Không tìm thấy file", f"File không tồn tại:\n{target}")
            return False

        try:
            doc = fitz.open(str(target))
            if len(doc) == 0:
                doc.close()
                QMessageBox.warning(self, "PDF trống", "Tài liệu PDF không có trang nào.")
                return False
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Lỗi đọc file PDF",
                f"Không thể mở file PDF (có thể bị hỏng hoặc đặt mật khẩu):\n{exc}",
            )
            return False

        self._close_pdf_doc()
        self._fitz_doc = doc
        self._input_pdf_path = target
        self._total_pages = len(doc)
        self._current_page_idx = 0
        self._last_result = None
        self._last_output_docx = None

        suggested_out = str(target.with_name(f"{target.stem}_converted.docx"))
        current_out = self.txt_output_path.text().strip()
        if not current_out or current_out == self._last_auto_output:
            self.txt_output_path.setText(suggested_out)
        self._last_auto_output = suggested_out

        size_str = _format_file_size(target.stat().st_size)
        self.lbl_pdf_name.setText(target.name)
        self.lbl_pdf_name.setToolTip(str(target))
        self.lbl_pdf_meta.setText(f"{self._total_pages} trang  •  {size_str}")
        self.lbl_ready_hint.setText(
            f"Đã sẵn sàng chuyển đổi {self._total_pages} trang từ '{target.name}'. Bấm nút bên dưới để chạy ngay."
        )

        self.left_stack.setCurrentIndex(1)
        self.right_stack.setCurrentIndex(0)
        self.render_preview_page(0)
        self._refresh_action_states()
        return True

    def render_preview_page(self, page_idx: int) -> None:
        """Render the requested 0-based PDF page into the left preview canvas."""
        if self._fitz_doc is None or self._total_pages <= 0:
            return
        idx = max(0, min(self._total_pages - 1, int(page_idx)))
        self._current_page_idx = idx
        page = self._fitz_doc[idx]
        pix = page.get_pixmap(dpi=144, alpha=False)
        qimg = QImage(
            pix.samples,
            pix.width,
            pix.height,
            pix.stride,
            QImage.Format.Format_RGB888,
        ).copy()
        self._raw_preview_pixmap = QPixmap.fromImage(qimg)
        self._rescale_preview_pixmap()
        self.lbl_page_nav.setText(f"Trang {idx + 1} / {self._total_pages}")
        self._refresh_action_states()

    def _rescale_preview_pixmap(self) -> None:
        if self._raw_preview_pixmap is None or self._raw_preview_pixmap.isNull():
            return
        target_w = max(180, self.lbl_preview_image.width() - 16)
        target_h = max(240, self.lbl_preview_image.height() - 16)
        scaled = self._raw_preview_pixmap.scaled(
            target_w,
            target_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.lbl_preview_image.setPixmap(scaled)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._rescale_preview_pixmap()

    def prev_page(self) -> None:
        if self._current_page_idx > 0:
            self.render_preview_page(self._current_page_idx - 1)

    def next_page(self) -> None:
        if self._current_page_idx < self._total_pages - 1:
            self.render_preview_page(self._current_page_idx + 1)

    def _update_elapsed_label(self) -> None:
        elapsed = max(0.0, time.perf_counter() - self._start_ts)
        mins, secs = divmod(elapsed, 60.0)
        self.lbl_elapsed_time.setText(f"Thời gian đã chạy: {int(mins):02d}:{secs:04.1f}s")

    def start_conversion(self) -> None:
        """Launch non-blocking background ConversionWorker."""
        if self._is_processing():
            return
        if self._input_pdf_path is None or not self._input_pdf_path.is_file():
            QMessageBox.information(self, "Chưa chọn file PDF", "Vui lòng chọn file PDF trước khi chuyển đổi.")
            return

        out_text = self.txt_output_path.text().strip()
        if not out_text:
            out_path = self._input_pdf_path.with_name(f"{self._input_pdf_path.stem}_converted.docx")
            self.txt_output_path.setText(str(out_path))
        else:
            out_path = Path(out_text)
            if out_path.suffix.lower() != ".docx":
                out_path = out_path.with_suffix(".docx")
                self.txt_output_path.setText(str(out_path))

        self.step_indicator.reset()
        self.progress_bar.setValue(0)
        self.lbl_proc_pct.setText("0%")
        self.lbl_proc_status.setText("Đang khởi động luồng chuyển đổi...")
        self.txt_activity_log.clear()
        self._last_log_msg = ""
        self.btn_cancel.setEnabled(True)
        self.btn_cancel.setText("Hủy xử lý (Cancel)")

        self._start_ts = time.perf_counter()
        self._elapsed_timer.start()
        self._update_elapsed_label()

        self.right_stack.setCurrentIndex(1)

        worker = ConversionWorker(
            input_pdf=self._input_pdf_path,
            output_docx=out_path,
            mode="editable",
            lang="vie",
            ocr_dpi=300,
            ocr_model=EngineSingletonManager.model_name or "vgg_transformer",
            parent=self,
        )
        worker.progress_updated.connect(self._on_progress_updated)
        worker.conversion_finished.connect(self._on_conversion_finished)
        worker.conversion_failed.connect(self._on_conversion_failed)
        worker.conversion_cancelled.connect(self._on_conversion_cancelled)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        self._refresh_action_states()
        worker.start()

    def _finalize_worker(self) -> None:
        if self._worker is not None:
            if self._worker.isRunning():
                self._worker.wait(3000)
            self._worker = None

    def cancel_conversion(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self.btn_cancel.setEnabled(False)
            self.btn_cancel.setText("Đang dừng an toàn...")
            self._worker.request_cancel()

    def _on_progress_updated(
        self,
        step_name: str,
        page_idx: int,
        total_pages: int,
        percent: int,
        message: str,
    ) -> None:
        del page_idx, total_pages
        clamped = max(0, min(100, int(percent)))
        self.progress_bar.setValue(clamped)
        self.lbl_proc_pct.setText(f"{clamped}%")
        if message:
            self.lbl_proc_status.setText(message)
            if message != self._last_log_msg:
                self.txt_activity_log.appendPlainText(message)
                self._last_log_msg = message
        self.step_indicator.set_active_step(step_name)

    def _on_conversion_finished(self, result: ConversionResult, elapsed_sec: float) -> None:
        self._elapsed_timer.stop()
        self._finalize_worker()
        self._last_result = result
        self._last_output_docx = Path(result.output_path)

        self.progress_bar.setValue(100)
        self.lbl_proc_pct.setText("100%")
        self.step_indicator.mark_all_done()

        nfc_text = "Đạt chuẩn NFC 100%" if result.nfc_verified else "Cần kiểm tra NFC"
        self.lbl_done_summary.setText(
            f"File đầu ra: {self._last_output_docx}\n"
            f"Tổng số trang: {result.pages_total} (OCR: {result.ocr_pages}, Văn bản gốc: {result.digital_pages})  •  "
            f"Bảng: {result.tables_count}  •  Đoạn văn: {result.paragraphs_count}\n"
            f"Tiếng Việt: {nfc_text}  •  Thời gian: {elapsed_sec:.2f} giây"
        )
        self.right_stack.setCurrentIndex(2)
        self._refresh_action_states()

    def _suggest_unlocked_output_path(self, locked_path: Path) -> Path:
        for idx in range(2, 50):
            cand = locked_path.with_name(f"{locked_path.stem}_v{idx}.docx")
            if not cand.exists():
                return cand
        return locked_path.with_name(f"{locked_path.stem}_copy.docx")

    def _on_conversion_failed(self, error_msg: str, is_permission_error: bool) -> None:
        self._elapsed_timer.stop()
        self._finalize_worker()
        self.right_stack.setCurrentIndex(0)
        self._refresh_action_states()

        if is_permission_error:
            locked = Path(self.txt_output_path.text().strip() or "output.docx")
            suggested = self._suggest_unlocked_output_path(locked)
            self.txt_output_path.setText(str(suggested))
            self._last_auto_output = str(suggested)
            QMessageBox.warning(
                self,
                "File Word đang được mở",
                "Không thể ghi đè file Word đích vì tệp đang được mở trong Microsoft Word hoặc ứng dụng khác.\n\n"
                f"Hệ thống đã tự động đổi đường dẫn gợi ý sang:\n{suggested}\n\n"
                "Vui lòng đóng file Word cũ hoặc bấm 'BẮT ĐẦU CHUYỂN ĐỔI' lại với tên mới.",
            )
        else:
            QMessageBox.critical(
                self,
                "Lỗi chuyển đổi PDF",
                f"Đã xảy ra lỗi trong quá trình chuyển đổi:\n{error_msg}",
            )

    def _on_conversion_cancelled(self, message: str) -> None:
        self._elapsed_timer.stop()
        self._finalize_worker()
        self.right_stack.setCurrentIndex(0)
        self._refresh_action_states()
        self.lbl_ready_hint.setText(message)

    def save_output_as(self, target_path: str | Path | None = None) -> Path | None:
        """Save or copy the generated `.docx` file to a user-selected path with lock protection."""
        if self._last_output_docx is None or not self._last_output_docx.is_file():
            QMessageBox.information(
                self, "Chưa có file kết quả", "Vui lòng chuyển đổi một file PDF trước khi lưu."
            )
            return None

        if target_path is None:
            chosen, _ = QFileDialog.getSaveFileName(
                self,
                "Lưu file Word (.docx)",
                str(self._last_output_docx),
                "Word Document (*.docx)",
            )
            if not chosen:
                return None
            dest = Path(chosen)
        else:
            dest = Path(target_path)

        if dest.suffix.lower() != ".docx":
            dest = dest.with_suffix(".docx")

        src = self._last_output_docx.resolve()
        dest_resolved = dest.resolve()
        try:
            dest_resolved.parent.mkdir(parents=True, exist_ok=True)
            if src != dest_resolved:
                shutil.copy2(str(src), str(dest_resolved))
            self._last_output_docx = dest_resolved
            self.txt_output_path.setText(str(dest_resolved))
            return dest_resolved
        except PermissionError:
            QMessageBox.warning(
                self,
                "Không thể lưu file",
                f"File '{dest_resolved.name}' đang bị khóa bởi Microsoft Word.\n"
                "Hãy đóng cửa sổ Word đang mở file này hoặc chọn lưu với một tên file khác.",
            )
            return None
        except OSError as exc:
            QMessageBox.critical(self, "Lỗi lưu file", f"Không thể lưu file Word:\n{exc}")
            return None

    def open_output_docx(self) -> None:
        """Open the generated `.docx` directly in Microsoft Word / default viewer."""
        if self._last_output_docx is None or not self._last_output_docx.is_file():
            return
        try:
            os.startfile(str(self._last_output_docx))
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Không thể mở file Word",
                f"Không thể khởi chạy trình xem Word cho:\n{self._last_output_docx}\n\nChi tiết: {exc}",
            )

    def open_output_folder(self) -> None:
        """Reveal the generated `.docx` inside Windows Explorer."""
        if self._last_output_docx is None or not self._last_output_docx.is_file():
            return
        try:
            subprocess.Popen(["explorer", "/select,", str(self._last_output_docx)])
        except Exception as exc:
            QMessageBox.warning(self, "Lỗi mở thư mục", f"Không thể mở Explorer:\n{exc}")

    def reset_for_new_file(self, open_dialog: bool = True) -> None:
        """Reset UI state for the next document while keeping warm OCR singletons in RAM."""
        if self._is_processing():
            return

        self._close_pdf_doc()
        self._input_pdf_path = None
        self._total_pages = 0
        self._current_page_idx = 0
        self._last_result = None
        self._last_output_docx = None
        self._last_auto_output = ""
        self._last_log_msg = ""

        self.txt_output_path.clear()
        self.lbl_pdf_name.setText("-")
        self.lbl_pdf_meta.setText("-")
        self.lbl_preview_image.clear()
        self.lbl_preview_image.setText("Chưa có trang xem trước")
        self.lbl_page_nav.setText("Trang 0 / 0")
        self.lbl_ready_hint.setText("Chọn file PDF ở cột bên trái để bắt đầu chuyển đổi sang Word.")
        self.progress_bar.setValue(0)
        self.lbl_proc_pct.setText("0%")
        self.step_indicator.reset()
        self.txt_activity_log.clear()

        self.left_stack.setCurrentIndex(0)
        self.right_stack.setCurrentIndex(0)
        self._refresh_action_states()

        if open_dialog:
            self.browse_pdf()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._elapsed_timer.stop()
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_cancel()
            self._worker.wait(5000)
        self._close_pdf_doc()
        super().closeEvent(event)
