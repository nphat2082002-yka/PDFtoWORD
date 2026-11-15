"""Headless offscreen integration tests for Office 365 Desktop UI & Singleton Preload."""

from __future__ import annotations

import gc
import os
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz
import pytest
from docx import Document

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import torch  # noqa: F401
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

import pdf2word
import pdf2word.analyzer as analyzer_mod
from pdf2word import convert_pdf_to_docx, get_ocr_engine
from ui import EngineSingletonManager, MainWindow, ModelPreloadWorker, SplashScreen
from ui.styles import APP_ICON_ICO, APP_ICON_PNG, AUTHOR_NAME, COPYRIGHT_TEXT


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    app.closeAllWindows()
    for widget in list(app.topLevelWidgets()):
        widget.deleteLater()
    app.processEvents()
    gc.collect()


def _pump_until(app: QApplication, predicate, timeout_ms: int = 180_000) -> bool:
    loop = QEventLoop()
    timed_out = False

    def _check():
        if predicate():
            loop.quit()

    def _on_timeout():
        nonlocal timed_out
        timed_out = True
        loop.quit()

    poll_timer = QTimer()
    poll_timer.setInterval(50)
    poll_timer.timeout.connect(_check)
    poll_timer.start()

    limit_timer = QTimer()
    limit_timer.setSingleShot(True)
    limit_timer.timeout.connect(_on_timeout)
    limit_timer.start(timeout_ms)

    if not predicate():
        loop.exec()

    poll_timer.stop()
    limit_timer.stop()
    app.processEvents()
    return not timed_out and bool(predicate())


def _create_sample_two_page_scan_pdf(dst_pdf: Path) -> Path:
    src_pdf = Path(r"D:\Project\PDFtoWord\samples\2026_2.pdf")
    if src_pdf.is_file():
        src = fitz.open(str(src_pdf))
        sub = fitz.open()
        try:
            indices = [0, len(src) - 1] if len(src) > 1 else [0]
            for idx in indices:
                sub.insert_pdf(src, from_page=idx, to_page=idx)
            sub.save(str(dst_pdf))
        finally:
            sub.close()
            src.close()
        return dst_pdf

    # Fallback: synthetic raster PDF if sample file is absent
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(img)
    draw.text((120, 120), "KE HOACH KIEM TRA", fill="black")
    pdf_doc = fitz.open()
    page = pdf_doc.new_page(width=595, height=842)
    img_bytes_path = dst_pdf.with_suffix(".png")
    img.save(str(img_bytes_path))
    page.insert_image(page.rect, filename=str(img_bytes_path))
    pdf_doc.save(str(dst_pdf))
    pdf_doc.close()
    img_bytes_path.unlink(missing_ok=True)
    return dst_pdf


def test_splash_screen_animation_and_progress(qt_app) -> None:
    assert APP_ICON_PNG.is_file() and APP_ICON_ICO.is_file()
    splash = SplashScreen()
    assert not splash.windowIcon().isNull()
    assert AUTHOR_NAME in splash.copyright_text
    assert splash.copyright_text == COPYRIGHT_TEXT
    splash.show()
    splash.set_progress(48, "Đang nạp mô hình VietOCR...")
    splash._advance_animation()
    qt_app.processEvents()

    pixmap = splash.grab()
    assert not pixmap.isNull()
    assert pixmap.width() == 680 and pixmap.height() == 420

    closed_called = False

    def _on_closed():
        nonlocal closed_called
        closed_called = True

    splash.set_progress(100, "Sẵn sàng!")
    splash.finish_and_close(_on_closed)
    qt_app.processEvents()

    assert closed_called is True
    assert not splash._timer.isActive()
    splash.deleteLater()
    qt_app.processEvents()


def test_model_preload_worker_and_singleton_caching(qt_app) -> None:
    progress_events: list[tuple[int, str]] = []
    finished_payloads: list[tuple[bool, str, str]] = []

    worker = ModelPreloadWorker(model_name="vgg_transformer")
    worker.progress_changed.connect(lambda pct, msg: progress_events.append((pct, msg)))
    worker.preload_finished.connect(lambda ok, dev, err: finished_payloads.append((ok, dev, err)))
    worker.start()

    ok = _pump_until(qt_app, lambda: len(finished_payloads) > 0, timeout_ms=240_000)
    worker.wait(5000)
    worker.deleteLater()
    qt_app.processEvents()

    assert ok, "ModelPreloadWorker timed out"
    success, device_label, err_msg = finished_payloads[0]
    assert success, f"Preload failed: {err_msg}"
    assert device_label in ("CPU", "GPU (CUDA)")
    assert progress_events and progress_events[-1][0] == 100
    assert EngineSingletonManager.is_ready is True
    assert EngineSingletonManager.engine is not None

    # Singleton check: both explicit model name and None must return exact cached instance
    eng_named = get_ocr_engine("vgg_transformer")
    eng_none = get_ocr_engine(None)
    assert eng_named is EngineSingletonManager.engine
    assert eng_none is EngineSingletonManager.engine
    assert eng_named.detector._ocr is not None
    assert eng_named.recognizer._predictor is not None


def test_main_window_preview_conversion_save_as_and_reset(qt_app, tmp_path: Path) -> None:
    win = MainWindow()
    win.show()
    qt_app.processEvents()

    assert not win.windowIcon().isNull()
    assert pdf2word.__author__ == AUTHOR_NAME
    assert AUTHOR_NAME in pdf2word.__copyright__
    assert AUTHOR_NAME in win.windowTitle()
    assert win.lbl_copyright.text() == COPYRIGHT_TEXT
    assert not hasattr(win, "btn_ribbon_open")
    assert not hasattr(win, "btn_ribbon_save")
    assert not hasattr(win, "btn_pick_pdf")
    assert not win.btn_change_pdf.isVisible()

    assert EngineSingletonManager.is_ready is True
    cached_engine_before = EngineSingletonManager.engine

    # 1. Load multi-page 2026_2.pdf (if present) and verify preview navigation
    sample_full = Path(r"D:\Project\PDFtoWord\samples\2026_2.pdf")
    if sample_full.is_file():
        assert win.load_pdf(sample_full) is True
        qt_app.processEvents()
        assert win.left_stack.currentIndex() == 1
        assert win.btn_change_pdf.isVisible() is True
        assert win.btn_pick_output.isVisibleTo(win) is True
        assert win.lbl_pdf_name.text() == "2026_2.pdf"
        assert win.lbl_page_nav.text() == f"Trang 1 / {win._total_pages}"
        assert win.lbl_preview_image.pixmap() is not None and not win.lbl_preview_image.pixmap().isNull()

        if win._total_pages > 1:
            win.next_page()
            assert win.lbl_page_nav.text() == f"Trang 2 / {win._total_pages}"
            win.prev_page()
            assert win.lbl_page_nav.text() == f"Trang 1 / {win._total_pages}"

    # 2. Load 2-page slice from 2026_2.pdf and run full GUI conversion workflow
    test_pdf = _create_sample_two_page_scan_pdf(tmp_path / "2026_slice.pdf")
    out_docx = tmp_path / "2026_slice_converted.docx"
    assert win.load_pdf(test_pdf) is True
    win.txt_output_path.setText(str(out_docx))

    recorded_steps: list[tuple[str, int, int, int, str]] = []
    win.start_conversion()
    assert win._worker is not None
    win._worker.progress_updated.connect(
        lambda step, p, t, pct, msg: recorded_steps.append((step, p, t, pct, msg))
    )

    done = _pump_until(
        qt_app,
        lambda: win._worker is None and win.right_stack.currentIndex() == 2,
        timeout_ms=240_000,
    )
    assert done, f"Conversion did not finish cleanly; status={win.lbl_proc_status.text()}"
    assert win.progress_bar.value() == 100
    assert not win.btn_change_pdf.isVisible()
    assert not win.btn_pick_output.isVisibleTo(win)
    assert win.btn_save_as.isVisibleTo(win)
    assert win.btn_new_file.isVisibleTo(win)
    assert out_docx.is_file() and out_docx.stat().st_size > 0

    # Verify all 5 steps & monotonically increasing 0 -> 100 coverage
    step_names = {item[0] for item in recorded_steps}
    assert {"preprocess", "detect", "recognize", "layout", "export"}.issubset(step_names), (
        f"Missing steps in callbacks: {step_names}"
    )
    percents = [item[3] for item in recorded_steps]
    assert percents[-1] == 100
    assert percents == sorted(percents)

    doc = Document(str(out_docx))
    full_txt = "\n".join(p.text for p in doc.paragraphs if p.text)
    table_txt = "\n".join(
        p.text for tbl in doc.tables for row in tbl.rows for cell in row.cells for p in cell.paragraphs if p.text
    )
    assert len(full_txt + table_txt) > 50

    # 3. Verify Save As + PermissionError handling when Word locks destination
    saved_copy = win.save_output_as(tmp_path / "custom_saved_word.docx")
    assert saved_copy is not None and saved_copy.is_file()

    with patch.object(QMessageBox, "warning") as mock_warn, patch(
        "ui.main_window.shutil.copy2", side_effect=PermissionError("Locked by Word")
    ):
        failed_save = win.save_output_as(tmp_path / "locked.docx")
        assert failed_save is None
        assert mock_warn.called

    # 4. Verify "Chọn file mới" resets UI state without dropping warm OCR singleton
    win.reset_for_new_file(open_dialog=False)
    qt_app.processEvents()
    assert win.left_stack.currentIndex() == 0
    assert win.right_stack.currentIndex() == 0
    assert win.progress_bar.value() == 0
    assert EngineSingletonManager.is_ready is True
    assert EngineSingletonManager.engine is cached_engine_before
    assert analyzer_mod.get_ocr_engine("vgg_transformer") is cached_engine_before

    win.close()
    win.deleteLater()
    qt_app.processEvents()


def test_convert_pdf_to_docx_without_callback_still_works(tmp_path: Path) -> None:
    pdf_path = tmp_path / "digital_compat.pdf"
    doc_pdf = fitz.open()
    page = doc_pdf.new_page(width=595, height=842)
    page.insert_text((72, 100), "CONG HOA XA HOI CHU NGHIA VIET NAM", fontsize=14)
    page.insert_text((72, 130), "Bao cao kiem thu tuong thich CLI khong co callback.", fontsize=12)
    doc_pdf.save(str(pdf_path))
    doc_pdf.close()

    out_docx = tmp_path / "digital_compat.docx"
    result = convert_pdf_to_docx(pdf_path, out_docx, mode="editable", lang="vie")
    assert out_docx.is_file()
    assert result.pages_total == 1
    assert result.nfc_verified is True
