"""Singleton OCR engine lifecycle and background QThread workers for PDFtoWord GUI."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from pdf2word import PaddleVietOcrEngine, convert_pdf_to_docx, preload_ocr_engine


class EngineSingletonManager:
    """Process-wide manager keeping preloaded & warmed PaddleOCR + VietOCR models in RAM."""

    engine: PaddleVietOcrEngine | None = None
    is_ready: bool = False
    device_label: str = "CPU"
    model_name: str = "vgg_transformer"

    @classmethod
    def ensure_loaded(
        cls,
        model_name: str | None = None,
        progress_callback=None,
    ) -> PaddleVietOcrEngine:
        """Load and warm up PaddleOCR + VietOCR once and reuse across subsequent files."""
        resolved = model_name or cls.model_name or "vgg_transformer"
        if cls.is_ready and cls.engine is not None and cls.model_name == resolved:
            if progress_callback is not None:
                progress_callback(100, "Sẵn sàng!")
            return cls.engine

        engine = preload_ocr_engine(model_name=resolved, progress_callback=progress_callback)
        cls.engine = engine
        cls.model_name = resolved
        cls.is_ready = True
        try:
            dev = str(engine.recognizer.device).lower()
            cls.device_label = "GPU (CUDA)" if "cuda" in dev else "CPU"
        except Exception:
            cls.device_label = "CPU"
        return engine


class ModelPreloadWorker(QThread):
    """Background QThread that preloads and warms up AI OCR + ADRT engines during splash."""

    progress_changed = Signal(int, str)
    preload_finished = Signal(bool, str, str)  # (success, device_label, error_message)

    def __init__(self, model_name: str = "vgg_transformer", parent=None) -> None:
        super().__init__(parent)
        self.model_name = model_name

    def run(self) -> None:
        try:
            EngineSingletonManager.ensure_loaded(
                model_name=self.model_name,
                progress_callback=self.progress_changed.emit,
            )
            self.preload_finished.emit(True, EngineSingletonManager.device_label, "")
        except Exception as exc:  # pragma: no cover - defensive runtime fallback
            self.preload_finished.emit(False, "CPU", f"{type(exc).__name__}: {exc}")


class ConversionWorker(QThread):
    """Non-blocking worker running `convert_pdf_to_docx` and emitting live step/page signals."""

    progress_updated = Signal(str, int, int, int, str)
    conversion_finished = Signal(object, float)
    conversion_failed = Signal(str, bool)  # (error_message, is_permission_error)
    conversion_cancelled = Signal(str)

    def __init__(
        self,
        input_pdf: str | Path,
        output_docx: str | Path,
        mode: str = "editable",
        lang: str = "vie",
        ocr_dpi: int = 300,
        ocr_model: str = "vgg_transformer",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.input_pdf = Path(input_pdf)
        self.output_docx = Path(output_docx)
        self.mode = mode
        self.lang = lang
        self.ocr_dpi = ocr_dpi
        self.ocr_model = ocr_model
        self.cancel_event = threading.Event()

    def request_cancel(self) -> None:
        """Signal the converter loop to stop cleanly before the next page."""
        self.cancel_event.set()

    def _emit_progress(
        self,
        step_name: str,
        page_idx: int,
        total_pages: int,
        percent: int,
        message: str,
    ) -> None:
        self.progress_updated.emit(step_name, page_idx, total_pages, percent, message)

    def run(self) -> None:
        start_ts = time.perf_counter()
        try:
            result = convert_pdf_to_docx(
                input_pdf=self.input_pdf,
                output_docx=self.output_docx,
                mode=self.mode,
                engine="cleanroom",
                lang=self.lang,
                ocr_dpi=self.ocr_dpi,
                ocr_model=self.ocr_model,
                progress_callback=self._emit_progress,
                cancel_event=self.cancel_event,
            )
            elapsed = time.perf_counter() - start_ts
            self.conversion_finished.emit(result, elapsed)
        except InterruptedError as exc:
            self.conversion_cancelled.emit(str(exc))
        except PermissionError as exc:
            self.conversion_failed.emit(str(exc), True)
        except Exception as exc:
            self.conversion_failed.emit(f"{type(exc).__name__}: {exc}", False)
