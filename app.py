"""Desktop entry point launching animated Splash Screen, AI warm-up, and Office UI."""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

# Import PyTorch before PaddlePaddle/Qt on Windows to prevent OpenMP/MKL DLL conflicts.
import torch  # noqa: F401


def _enable_windows_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PDFtoWord.VNOffice.Pro.1")
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _ensure_valid_stdio() -> None:
    """Redirect None stdio streams when running inside a windowless PyInstaller GUI .exe."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Launch VN-Office PDF to Word Pro desktop GUI."""
    _ensure_valid_stdio()
    _enable_windows_dpi_awareness()
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    args = sys.argv[1:] if argv is None else argv
    smoke_test = "--smoke-test" in args
    smoke_ocr = "--smoke-ocr" in args
    if smoke_test or smoke_ocr:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    from PySide6.QtWidgets import QApplication, QMessageBox

    from ui import MainWindow, ModelPreloadWorker, SplashScreen
    from ui.styles import APP_VERSION, AUTHOR_NAME, load_app_icon

    pdf_args = [a for a in args if not a.startswith("-")]
    initial_pdf = Path(pdf_args[0]).resolve() if pdf_args and Path(pdf_args[0]).is_file() else None

    app = QApplication(sys.argv)
    app.setApplicationName("VN-Office PDF to Word Pro")
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(AUTHOR_NAME)
    app_icon = load_app_icon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)

    splash = SplashScreen()
    splash.show()

    main_window = MainWindow()

    if smoke_test or smoke_ocr:
        import tempfile

        log_path = Path(tempfile.gettempdir()) / "vn_office_pdf2word_smoke.log"
        try:
            splash.set_progress(100, "Smoke test OK")
            app.processEvents()
            assert AUTHOR_NAME in main_window.windowTitle()
            assert AUTHOR_NAME in main_window.lbl_copyright.text()
            assert not app_icon.isNull()
            if smoke_ocr:
                from pdf2word import preload_ocr_engine

                eng = preload_ocr_engine(model_name="vgg_transformer")
                assert eng.detector._ocr is not None
                assert eng.recognizer._predictor is not None
            main_window.show()
            app.processEvents()
            splash.close()
            main_window.close()
            splash.deleteLater()
            main_window.deleteLater()
            app.processEvents()
            log_path.write_text("OK\n", encoding="utf-8")
            return 0
        except Exception as exc:
            import traceback

            log_path.write_text(f"FAILED: {exc}\n{traceback.format_exc()}", encoding="utf-8")
            return 1

    preload_worker = ModelPreloadWorker(model_name="vgg_transformer")

    def _on_preload_progress(pct: int, msg: str) -> None:
        splash.set_progress(pct, msg)

    def _on_preload_finished(success: bool, device_label: str, error_msg: str) -> None:
        main_window.update_engine_status(success, device_label)
        if success:
            splash.set_progress(100, "Sẵn sàng!")

        def _reveal_main() -> None:
            main_window.show()
            main_window.raise_()
            main_window.activateWindow()
            if initial_pdf is not None:
                main_window.load_pdf(initial_pdf)
            if not success and error_msg:
                QMessageBox.warning(
                    main_window,
                    "Cảnh báo nạp trước AI Engine",
                    "Không thể nạp sẵn toàn bộ model OCR trong lúc khởi động.\n"
                    "Ứng dụng vẫn mở và sẽ thử nạp lại khi chuyển đổi:\n\n"
                    f"{error_msg}",
                )

        splash.finish_and_close(_reveal_main)

    preload_worker.progress_changed.connect(_on_preload_progress)
    preload_worker.preload_finished.connect(_on_preload_finished)
    preload_worker.start()

    exit_code = app.exec()
    if preload_worker.isRunning():
        preload_worker.wait(5000)
    return int(exit_code)


if __name__ == "__main__":
    sys.exit(main())
