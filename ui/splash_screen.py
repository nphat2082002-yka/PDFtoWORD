"""Frameless animated PDF-to-Word Splash Screen rendered at 60 FPS via QPainter."""

from __future__ import annotations

import math

from PySide6.QtCore import QEasingCurve, QPointF, QPropertyAnimation, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication, QGraphicsOpacityEffect, QWidget

from .styles import COPYRIGHT_TEXT, load_app_icon


class SplashScreen(QWidget):
    """Frameless Fluent-style splash screen with 60 FPS PDF laser scan -> Word animation."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.SplashScreen
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(680, 420)
        icon = load_app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)

        self._anim_phase: float = 0.0
        self._progress_pct: int = 0
        self._status_text: str = "Đang khởi tạo môi trường AI..."
        self.copyright_text: str = COPYRIGHT_TEXT
        self._fade_anim: QPropertyAnimation | None = None

        self._timer = QTimer(self)
        self._timer.setInterval(16)  # ~60 FPS
        self._timer.timeout.connect(self._advance_animation)
        self._timer.start()

        self._center_on_screen()

    def _center_on_screen(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        x = geo.x() + (geo.width() - self.width()) // 2
        y = geo.y() + (geo.height() - self.height()) // 2
        self.move(x, y)

    def _advance_animation(self) -> None:
        self._anim_phase = (self._anim_phase + 0.015) % 1.0
        self.update()

    def set_progress(self, percent: int, message: str) -> None:
        """Update splash preload percentage and status label safely."""
        self._progress_pct = max(0, min(100, int(percent)))
        if message:
            self._status_text = message
        self.update()

    def finish_and_close(self, on_closed_callback=None) -> None:
        """Stop animation loop cleanly, fade out smoothly, and invoke callback."""
        if self._timer.isActive():
            self._timer.stop()

        platform = (QApplication.platformName() or "").lower()
        if platform == "offscreen":
            if on_closed_callback is not None:
                on_closed_callback()
            self.close()
            return

        effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(260)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        def _done() -> None:
            if on_closed_callback is not None:
                on_closed_callback()
            self.close()

        anim.finished.connect(_done)
        self._fade_anim = anim
        anim.start()

    def closeEvent(self, event) -> None:
        if self._timer.isActive():
            self._timer.stop()
        super().closeEvent(event)

    def _draw_document_base(self, painter: QPainter, rect: QRectF, fold: float = 18.0) -> None:
        path = QPainterPath()
        path.moveTo(rect.left() + 8, rect.top())
        path.lineTo(rect.right() - fold, rect.top())
        path.lineTo(rect.right(), rect.top() + fold)
        path.lineTo(rect.right(), rect.bottom() - 8)
        path.quadTo(rect.right(), rect.bottom(), rect.right() - 8, rect.bottom())
        path.lineTo(rect.left() + 8, rect.bottom())
        path.quadTo(rect.left(), rect.bottom(), rect.left(), rect.bottom() - 8)
        path.lineTo(rect.left(), rect.top() + 8)
        path.quadTo(rect.left(), rect.top(), rect.left() + 8, rect.top())
        path.closeSubpath()

        # Subtle card shadow
        painter.save()
        painter.translate(0, 4)
        painter.fillPath(path, QColor(0, 0, 0, 45))
        painter.restore()

        painter.fillPath(path, QColor("#FFFFFF"))

        # Dog-ear fold corner
        fold_path = QPainterPath()
        fold_path.moveTo(rect.right() - fold, rect.top())
        fold_path.lineTo(rect.right() - fold, rect.top() + fold)
        fold_path.lineTo(rect.right(), rect.top() + fold)
        fold_path.closeSubpath()
        painter.fillPath(fold_path, QColor("#E2E8F0"))

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )

        # 1. Outer soft drop shadow & rounded Fluent gradient card
        card_rect = QRectF(16, 14, self.width() - 32, self.height() - 30)
        for idx, alpha in ((6, 18), (4, 28), (2, 42)):
            shadow_rect = card_rect.adjusted(-idx, -idx + 3, idx, idx + 4)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(11, 25, 44, alpha))
            painter.drawRoundedRect(shadow_rect, 18, 18)

        card_path = QPainterPath()
        card_path.addRoundedRect(card_rect, 16, 16)
        bg_grad = QLinearGradient(card_rect.topLeft(), card_rect.bottomRight())
        bg_grad.setColorAt(0.0, QColor("#0B192C"))
        bg_grad.setColorAt(0.52, QColor("#103A75"))
        bg_grad.setColorAt(1.0, QColor("#185ABD"))
        painter.fillPath(card_path, bg_grad)

        # Ambient glow accent in upper-right corner
        painter.save()
        painter.setClipPath(card_path)
        glow = QRadialGradient(QPointF(card_rect.right() - 70, card_rect.top() + 50), 220)
        glow.setColorAt(0.0, QColor(56, 189, 248, 55))
        glow.setColorAt(1.0, QColor(56, 189, 248, 0))
        painter.fillRect(card_rect, glow)
        painter.restore()

        painter.setPen(QPen(QColor(255, 255, 255, 42), 1.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(card_rect, 16, 16)

        # Smooth scan sweep factor in [0, 1]
        scan_t = 0.5 - 0.5 * math.cos(2.0 * math.pi * self._anim_phase)

        # 2. Left Sheet: PDF Document + Scanning Laser Beam
        pdf_rect = QRectF(card_rect.center().x() - 185, card_rect.top() + 44, 118, 152)
        self._draw_document_base(painter, pdf_rect)

        pdf_header = QRectF(pdf_rect.left() + 10, pdf_rect.top() + 14, 52, 22)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#E53935"))
        painter.drawRoundedRect(pdf_header, 5, 5)
        painter.setPen(QColor("#FFFFFF"))
        painter.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        painter.drawText(pdf_header, Qt.AlignmentFlag.AlignCenter, "PDF")

        # Simulated PDF text lines (light up when laser passes over them)
        scan_y = pdf_rect.top() + 46 + scan_t * (pdf_rect.height() - 58)
        line_specs = (0.78, 0.86, 0.68, 0.90, 0.74, 0.84, 0.58, 0.80)
        for i, ratio in enumerate(line_specs):
            ly = pdf_rect.top() + 48 + i * 12
            line_rect = QRectF(pdf_rect.left() + 12, ly, (pdf_rect.width() - 24) * ratio, 5)
            passed = scan_y >= ly
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#38BDF8" if passed else "#CBD5E1"))
            painter.drawRoundedRect(line_rect, 2.5, 2.5)

        # Red mini stamp dot on PDF bottom-right
        painter.setPen(QPen(QColor(229, 57, 53, 180), 1.6))
        painter.setBrush(QColor(229, 57, 53, 35))
        painter.drawEllipse(QPointF(pdf_rect.right() - 22, pdf_rect.bottom() - 20), 10, 10)

        # Laser aura & glowing beam across PDF sheet
        beam_rect = QRectF(pdf_rect.left() - 6, scan_y - 12, pdf_rect.width() + 12, 24)
        beam_grad = QLinearGradient(beam_rect.topLeft(), beam_rect.bottomLeft())
        beam_grad.setColorAt(0.0, QColor(56, 189, 248, 0))
        beam_grad.setColorAt(0.45, QColor(56, 189, 248, 125))
        beam_grad.setColorAt(0.50, QColor(255, 82, 82, 235))
        beam_grad.setColorAt(0.55, QColor(56, 189, 248, 125))
        beam_grad.setColorAt(1.0, QColor(56, 189, 248, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.fillRect(beam_rect, beam_grad)

        painter.setPen(QPen(QColor("#E0F2FE"), 2.0))
        painter.drawLine(
            QPointF(pdf_rect.left() - 8, scan_y),
            QPointF(pdf_rect.right() + 8, scan_y),
        )

        # 3. Center Transformation Stream (Particles & Animated Chevrons)
        stream_left = pdf_rect.right() + 14
        stream_right = card_rect.center().x() + 67 - 14
        stream_mid_y = pdf_rect.center().y()
        for p_idx in range(5):
            pt_t = (self._anim_phase + p_idx * 0.20) % 1.0
            px = stream_left + pt_t * (stream_right - stream_left)
            py = stream_mid_y + math.sin(pt_t * math.pi * 2.0 + p_idx) * 10.0
            alpha = int(255 * math.sin(pt_t * math.pi))
            radius = 3.2 + 1.8 * math.sin(pt_t * math.pi)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(125, 211, 252, max(0, alpha)))
            painter.drawEllipse(QPointF(px, py), radius, radius)

        for idx in range(3):
            pulse = 0.45 + 0.55 * max(
                0.0, math.sin(2.0 * math.pi * self._anim_phase - idx * 0.85)
            )
            cx = stream_left + (idx + 1) * (stream_right - stream_left) / 4.0
            pen = QPen(QColor(224, 242, 254, int(240 * pulse)), 2.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(QPointF(cx - 5, stream_mid_y - 6), QPointF(cx + 2, stream_mid_y))
            painter.drawLine(QPointF(cx - 5, stream_mid_y + 6), QPointF(cx + 2, stream_mid_y))

        # 4. Right Sheet: Microsoft Word DOCX Icon & Structured Layout Blocks
        doc_rect = QRectF(card_rect.center().x() + 67, card_rect.top() + 44, 118, 152)
        self._draw_document_base(painter, doc_rect)

        w_badge = QRectF(doc_rect.left() + 10, doc_rect.top() + 14, 24, 24)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#185ABD"))
        painter.drawRoundedRect(w_badge, 5, 5)
        painter.setPen(QColor("#FFFFFF"))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        painter.drawText(w_badge, Qt.AlignmentFlag.AlignCenter, "W")

        docx_lbl_rect = QRectF(w_badge.right() + 6, doc_rect.top() + 14, 48, 24)
        painter.setPen(QColor("#185ABD"))
        painter.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        painter.drawText(docx_lbl_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "DOCX")

        # Structured 2-col header + paragraph lines illuminating in sync
        active_color = QColor("#2563EB")
        idle_color = QColor("#DBEAFE")
        block_defs = (
            (doc_rect.top() + 48, 0.42, 0.0),
            (doc_rect.top() + 48, 0.42, 0.52),
            (doc_rect.top() + 64, 0.86, 0.0),
            (doc_rect.top() + 78, 0.92, 0.0),
            (doc_rect.top() + 92, 0.78, 0.0),
            (doc_rect.top() + 108, 0.90, 0.0),
            (doc_rect.top() + 122, 0.65, 0.0),
        )
        usable_w = doc_rect.width() - 24
        for idx, (by, w_ratio, x_off_ratio) in enumerate(block_defs):
            lit = scan_t >= (idx / len(block_defs)) * 0.85
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(active_color if lit else idle_color)
            painter.drawRoundedRect(
                QRectF(doc_rect.left() + 12 + usable_w * x_off_ratio, by, usable_w * w_ratio, 7),
                3,
                3,
            )

        # 5. Title, Subtitle, Live Preload Status & Progress Bar
        title_rect = QRectF(card_rect.left() + 28, card_rect.top() + 216, card_rect.width() - 56, 36)
        painter.setPen(QColor("#FFFFFF"))
        painter.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignCenter, "VN-Office PDF to Word Pro")

        sub_rect = QRectF(card_rect.left() + 28, card_rect.top() + 250, card_rect.width() - 56, 22)
        painter.setPen(QColor("#BFDBFE"))
        painter.setFont(QFont("Segoe UI", 9.5))
        painter.drawText(
            sub_rect,
            Qt.AlignmentFlag.AlignCenter,
            "AI ADRT Layout Engine • PaddleOCR + VietOCR Vietnamese Edition",
        )

        copy_rect = QRectF(card_rect.left() + 28, card_rect.top() + 276, card_rect.width() - 56, 22)
        painter.setPen(QColor("#93C5FD"))
        painter.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
        painter.drawText(copy_rect, Qt.AlignmentFlag.AlignCenter, self.copyright_text)

        # Status text + Percentage
        status_rect = QRectF(card_rect.left() + 34, card_rect.bottom() - 62, card_rect.width() - 120, 24)
        painter.setPen(QColor("#E0F2FE"))
        painter.setFont(QFont("Segoe UI", 9.5))
        elided = painter.fontMetrics().elidedText(
            self._status_text, Qt.TextElideMode.ElideRight, int(status_rect.width())
        )
        painter.drawText(status_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)

        pct_rect = QRectF(card_rect.right() - 84, card_rect.bottom() - 62, 50, 24)
        painter.setPen(QColor("#FFFFFF"))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        painter.drawText(
            pct_rect,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
            f"{self._progress_pct}%",
        )

        # Rounded Progress Bar Track + Animated Gradient Fill
        bar_rect = QRectF(card_rect.left() + 34, card_rect.bottom() - 32, card_rect.width() - 68, 10)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 38))
        painter.drawRoundedRect(bar_rect, 5, 5)

        if self._progress_pct > 0:
            fill_w = max(10.0, bar_rect.width() * (self._progress_pct / 100.0))
            fill_rect = QRectF(bar_rect.left(), bar_rect.top(), fill_w, bar_rect.height())
            fill_grad = QLinearGradient(fill_rect.topLeft(), fill_rect.topRight())
            fill_grad.setColorAt(0.0, QColor("#38BDF8"))
            fill_grad.setColorAt(1.0, QColor("#93C5FD"))
            painter.setBrush(fill_grad)
            painter.drawRoundedRect(fill_rect, 5, 5)
