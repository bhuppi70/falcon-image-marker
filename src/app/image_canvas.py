"""Widget for displaying an image and placing a vertical marker line."""

from __future__ import annotations

from PIL import Image, ImageOps
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QWidget


class ImageCanvas(QWidget):
    """Displays an image scaled to fit; left-click sets a vertical marker, right-click clears it."""

    marker_changed = pyqtSignal(object)      # (x0, y0, x1, y1) image-px tuple, or None
    line_first_point = pyqtSignal(object)   # (img_x, img_y) after first click, or None
    rect_changed = pyqtSignal(object)       # (x0, y0, x1, y1) image-px tuple, or None
    rect_first_point = pyqtSignal(object)   # (img_x, img_y) after first click, or None

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._line_p1: tuple[float, float] | None = None               # first click, image coords
        self._line_pts: tuple[float, float, float, float] | None = None  # final line
        self._show_perimeter: bool = False
        self._line_mode: bool = False
        self._rect_mode: bool = False
        self._rect_p1: tuple[float, float] | None = None          # first click, image coords
        self._rect_image: tuple[float, float, float, float] | None = None  # final rect
        self._cursor_widget_pos: tuple[int, int] | None = None    # live mouse pos for preview
        self.setMinimumSize(400, 300)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_image(self, path: str) -> bool:
        pixmap = _load_pixmap(path)
        if pixmap is None:
            return False
        self._pixmap = pixmap
        self._line_p1 = None
        self._line_pts = None
        self.line_first_point.emit(None)
        self.rect_first_point.emit(None)
        if self._rect_image is not None or self._rect_p1 is not None:
            self._rect_p1 = None
            self._rect_image = None
            self.rect_changed.emit(None)
        self.update()
        self.marker_changed.emit(None)
        return True

    def set_perimeter(self, enabled: bool) -> None:
        self._show_perimeter = enabled
        self.update()

    def set_line_mode(self, enabled: bool) -> None:
        self._line_mode = enabled
        if not enabled:
            self._line_p1 = None
            self._line_pts = None
            self._cursor_widget_pos = None
            self.line_first_point.emit(None)
            self.marker_changed.emit(None)
        self.update()

    def set_rect_mode(self, enabled: bool) -> None:
        self._rect_mode = enabled
        if not enabled:
            self._rect_p1 = None
            self._rect_image = None
            self._cursor_widget_pos = None
            self.rect_first_point.emit(None)
            self.rect_changed.emit(None)
        self.update()

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

        if self._line_pts is not None:
            x0i, y0i, x1i, y1i = self._line_pts
            wx0 = self._image_x_to_widget_x(x0i, img_rect)
            wy0 = self._image_y_to_widget_y(y0i, img_rect)
            wx1 = self._image_x_to_widget_x(x1i, img_rect)
            wy1 = self._image_y_to_widget_y(y1i, img_rect)
            painter.setPen(QPen(QColor(0, 255, 0), 3, Qt.PenStyle.SolidLine))
            painter.drawLine(wx0, wy0, wx1, wy1)

            if self._show_perimeter:
                painter.setPen(QPen(QColor(0, 255, 0), 2, Qt.PenStyle.SolidLine))
                painter.drawRect(img_rect)

        if self._line_p1 is not None:
            px = self._image_x_to_widget_x(self._line_p1[0], img_rect)
            py = self._image_y_to_widget_y(self._line_p1[1], img_rect)

            if self._cursor_widget_pos is not None:
                cx, cy = self._cursor_widget_pos
                painter.setPen(QPen(QColor(0, 255, 0), 1, Qt.PenStyle.DashLine))
                painter.drawLine(px, py, cx, cy)

            painter.setPen(QPen(QColor(0, 255, 0), 2))
            painter.drawLine(px - 10, py, px + 10, py)
            painter.drawLine(px, py - 10, px, py + 10)

        if self._rect_image is not None:
            x0i, y0i, x1i, y1i = self._rect_image
            rx0 = self._image_x_to_widget_x(x0i, img_rect)
            rx1 = self._image_x_to_widget_x(x1i, img_rect)
            ry0 = self._image_y_to_widget_y(y0i, img_rect)
            ry1 = self._image_y_to_widget_y(y1i, img_rect)
            painter.setPen(QPen(QColor(0, 255, 0), 2, Qt.PenStyle.SolidLine))
            painter.drawRect(min(rx0, rx1), min(ry0, ry1),
                             abs(rx1 - rx0), abs(ry1 - ry0))

        if self._rect_p1 is not None:
            px = self._image_x_to_widget_x(self._rect_p1[0], img_rect)
            py = self._image_y_to_widget_y(self._rect_p1[1], img_rect)

            if self._cursor_widget_pos is not None:
                cx, cy = self._cursor_widget_pos
                painter.setBrush(QColor(0, 255, 0, 50))
                painter.setPen(QPen(QColor(0, 255, 0), 1, Qt.PenStyle.DashLine))
                painter.drawRect(min(px, cx), min(py, cy), abs(cx - px), abs(cy - py))
                painter.setBrush(Qt.BrushStyle.NoBrush)

            painter.setPen(QPen(QColor(0, 255, 0), 2))
            painter.drawLine(px - 10, py, px + 10, py)
            painter.drawLine(px, py - 10, px, py + 10)

    def mousePressEvent(self, event):
        if self._pixmap is None:
            return
        _, img_rect = self._scaled_pixmap_and_rect()
        pos = event.position().toPoint()

        if self._rect_mode:
            if event.button() == Qt.MouseButton.LeftButton and img_rect.contains(pos):
                scale = self._pixmap.width() / img_rect.width()
                img_x = (pos.x() - img_rect.left()) * scale
                img_y = (pos.y() - img_rect.top()) * scale
                if self._rect_p1 is None:
                    if self._rect_image is not None:
                        self._rect_image = None
                        self.rect_changed.emit(None)
                    self._rect_p1 = (img_x, img_y)
                    self.rect_first_point.emit(self._rect_p1)
                else:
                    x0, y0 = self._rect_p1
                    self._rect_image = (x0, y0, img_x, img_y)
                    self._rect_p1 = None
                    self.rect_first_point.emit(None)
                    self.rect_changed.emit(self._rect_image)
                self.update()
            elif event.button() == Qt.MouseButton.RightButton:
                if self._rect_p1 is not None:
                    self._rect_p1 = None
                    self.rect_first_point.emit(None)
                    self.update()
                elif self._rect_image is not None:
                    self._rect_image = None
                    self.rect_changed.emit(None)
                    self.update()
            return

        if self._line_mode:
            if event.button() == Qt.MouseButton.LeftButton and img_rect.contains(pos):
                scale = self._pixmap.width() / img_rect.width()
                img_x = (pos.x() - img_rect.left()) * scale
                img_y = (pos.y() - img_rect.top()) * scale
                if self._line_p1 is None:
                    if self._line_pts is not None:
                        self._line_pts = None
                        self.marker_changed.emit(None)
                    self._line_p1 = (img_x, img_y)
                    self.line_first_point.emit(self._line_p1)
                else:
                    x0, y0 = self._line_p1
                    self._line_pts = (x0, y0, img_x, img_y)
                    self._line_p1 = None
                    self.line_first_point.emit(None)
                    self.marker_changed.emit(self._line_pts)
                self.update()
            elif event.button() == Qt.MouseButton.RightButton:
                if self._line_p1 is not None:
                    self._line_p1 = None
                    self.line_first_point.emit(None)
                    self.update()
                elif self._line_pts is not None:
                    self._line_pts = None
                    self.marker_changed.emit(None)
                    self.update()
            return

    def mouseMoveEvent(self, event):
        if (self._rect_mode and self._rect_p1 is not None) or \
                (self._line_mode and self._line_p1 is not None):
            pos = event.position().toPoint()
            self._cursor_widget_pos = (pos.x(), pos.y())
            self.update()

    def leaveEvent(self, event):
        if self._cursor_widget_pos is not None:
            self._cursor_widget_pos = None
            self.update()

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

    def _image_y_to_widget_y(self, image_y: float, img_rect) -> int:
        scale = img_rect.height() / self._pixmap.height()
        return round(img_rect.top() + image_y * scale)


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
