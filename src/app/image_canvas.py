"""Widget for displaying an image and placing a vertical marker line."""

from __future__ import annotations

from PIL import Image, ImageOps
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QWidget


class ImageCanvas(QWidget):
    """Displays an image scaled to fit; left-click sets a vertical marker, right-click clears it."""

    marker_changed = pyqtSignal(object)  # float image-pixel x, or None when cleared

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._marker_image_x: float | None = None  # stored in image-pixel coords
        self.setMinimumSize(400, 300)
        self.setCursor(Qt.CursorShape.CrossCursor)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_image(self, path: str) -> bool:
        pixmap = _load_pixmap(path)
        if pixmap is None:
            return False
        self._pixmap = pixmap
        self._marker_image_x = None
        self.update()
        self.marker_changed.emit(None)
        return True

    def clear_marker(self) -> None:
        self._marker_image_x = None
        self.update()
        self.marker_changed.emit(None)

    @property
    def image_size(self) -> tuple[int, int] | None:
        if self._pixmap is None:
            return None
        return self._pixmap.width(), self._pixmap.height()

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor(30, 30, 30))

        if self._pixmap is None:
            painter.setPen(QColor(160, 160, 160))
            font = QFont()
            font.setPointSize(14)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Open an image to get started")
            return

        scaled, img_rect = self._scaled_pixmap_and_rect()
        painter.drawPixmap(img_rect.topLeft(), scaled)

        if self._marker_image_x is not None:
            widget_x = self._image_x_to_widget_x(self._marker_image_x, img_rect)

            pen = QPen(QColor(0, 255, 0), 3, Qt.PenStyle.SolidLine)
            painter.setPen(pen)
            painter.drawLine(widget_x, img_rect.top(), widget_x, img_rect.bottom())

            border_pen = QPen(QColor(0, 255, 0), 2, Qt.PenStyle.DashLine)
            border_pen.setDashPattern([8, 5])
            painter.setPen(border_pen)
            painter.drawRect(img_rect)

    def mousePressEvent(self, event):
        if self._pixmap is None:
            return
        _, img_rect = self._scaled_pixmap_and_rect()
        pos = event.position().toPoint()

        if event.button() == Qt.MouseButton.LeftButton and img_rect.contains(pos):
            scale = self._pixmap.width() / img_rect.width()
            self._marker_image_x = (pos.x() - img_rect.left()) * scale
            self.update()
            self.marker_changed.emit(self._marker_image_x)
        elif event.button() == Qt.MouseButton.RightButton:
            self.clear_marker()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _scaled_pixmap_and_rect(self):
        scaled = self._pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        return scaled, scaled.rect().translated(x, y)

    def _image_x_to_widget_x(self, image_x: float, img_rect) -> int:
        scale = img_rect.width() / self._pixmap.width()
        return round(img_rect.left() + image_x * scale)


def _load_pixmap(path: str) -> QPixmap | None:
    """Load an image via Pillow, applying EXIF orientation before converting to QPixmap."""
    try:
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGBA")
    except Exception:
        return None
    data = img.tobytes("raw", "RGBA")
    qimage = QImage(data, img.width, img.height, img.width * 4, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qimage)
