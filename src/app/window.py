"""Main application window."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QAction, QCloseEvent
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .image_canvas import ImageCanvas
from .laser_output import HeliosOutput

_DEFAULT_LOGO = Path(__file__).parent.parent.parent / "Bigfoot2.svg"

_VERSION = "1.2.0"

_IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.bmp *.gif *.tiff *.tif *.webp);;All Files (*)"

_BUTTON_STYLE = """
    QPushButton {
        padding: 6px 20px;
        font-size: 13px;
        border-radius: 6px;
        background-color: #3a3a3a;
        color: #f0f0f0;
        border: 1px solid #555;
    }
    QPushButton:hover {
        background-color: #4a4a4a;
        border-color: #888;
    }
    QPushButton:pressed {
        background-color: #222;
    }
    QPushButton:disabled {
        color: #666;
        border-color: #444;
    }
    QPushButton:checked {
        background-color: #1a4d1a;
        border-color: #00cc44;
        color: #00ff55;
    }
"""

_LASER_CONNECTED_STYLE = "color: #00cc44; font-size: 12px;"
_LASER_DISCONNECTED_STYLE = "color: #888; font-size: 12px;"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Image Marker (v{_VERSION})")
        self.resize(1000, 750)

        self._current_name: str = ""
        self._image_width: int = 1
        self._image_height: int = 1

        # --- Laser DAC ---
        self._laser = HeliosOutput()
        laser_ok = self._laser.open()

        # --- Button bar ---
        self._btn_open = QPushButton("Open Image")
        self._btn_open.setStyleSheet(_BUTTON_STYLE)
        self._btn_open.clicked.connect(self._open_image)

        self._btn_mark_line = QPushButton("Mark Line")
        self._btn_mark_line.setStyleSheet(_BUTTON_STYLE)
        self._btn_mark_line.setCheckable(True)
        self._btn_mark_line.setChecked(False)
        self._btn_mark_line.toggled.connect(self._on_mark_line_toggled)

        self._btn_perimeter = QPushButton("Perimeter")
        self._btn_perimeter.setStyleSheet(_BUTTON_STYLE)
        self._btn_perimeter.setCheckable(True)
        self._btn_perimeter.setChecked(False)
        self._btn_perimeter.toggled.connect(self._on_perimeter_toggled)

        self._btn_logo = QPushButton("Logo")
        self._btn_logo.setStyleSheet(_BUTTON_STYLE)
        self._btn_logo.setCheckable(True)
        self._btn_logo.setChecked(False)
        self._btn_logo.toggled.connect(self._on_logo_toggled)

        self._btn_mark_rect = QPushButton("Mark Rectangle")
        self._btn_mark_rect.setStyleSheet(_BUTTON_STYLE)
        self._btn_mark_rect.setCheckable(True)
        self._btn_mark_rect.setChecked(False)
        self._btn_mark_rect.toggled.connect(self._on_mark_rect_toggled)

        if laser_ok:
            n = self._laser.device_count
            laser_text = f"Laser: Connected ({n} device{'s' if n != 1 else ''})"
            laser_style = _LASER_CONNECTED_STYLE
        else:
            laser_text = "Laser: Not connected"
            laser_style = _LASER_DISCONNECTED_STYLE

        self._laser_label = QLabel(laser_text)
        self._laser_label.setStyleSheet(laser_style)

        btn_bar = QHBoxLayout()
        btn_bar.setContentsMargins(10, 8, 10, 8)
        btn_bar.setSpacing(10)
        btn_bar.addWidget(self._btn_open)
        btn_bar.addWidget(self._btn_mark_line)
        btn_bar.addWidget(self._btn_mark_rect)
        btn_bar.addWidget(self._btn_perimeter)
        btn_bar.addWidget(self._btn_logo)
        btn_bar.addStretch()

        # --- Canvas ---
        self._canvas = ImageCanvas()
        self._canvas.marker_changed.connect(self._on_marker_changed)
        self._canvas.rect_changed.connect(self._on_rect_changed)

        # --- Root layout ---
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(btn_bar)
        layout.addWidget(self._canvas)
        self.setCentralWidget(root)

        # --- Status bar ---
        self._status = QLabel("Open an image to get started")
        self.statusBar().addWidget(self._status)
        self.statusBar().addPermanentWidget(self._laser_label)

        self._build_menu()

        # Auto-load default logo so the laser projects immediately on startup
        if laser_ok and _DEFAULT_LOGO.exists() and self._laser.load_logo(str(_DEFAULT_LOGO)):
            self._btn_logo.setChecked(True)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._laser.close()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------

    def _build_menu(self):
        file_menu = self.menuBar().addMenu("&File")

        open_act = QAction("&Open Image…", self)
        open_act.setShortcut("Ctrl+O")
        open_act.triggered.connect(self._open_image)
        file_menu.addAction(open_act)

        file_menu.addSeparator()

        quit_act = QAction("&Quit", self)
        quit_act.setShortcut("Ctrl+Q")
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _open_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Image", str(Path.home()), _IMAGE_FILTER
        )
        if not path:
            return
        if not self._canvas.load_image(path):
            self._status.setText(f"Failed to load: {path}")
            return

        if self._btn_logo.isChecked():
            self._btn_logo.setChecked(False)

        self._current_name = Path(path).name
        self._image_width, self._image_height = self._canvas.image_size
        self.setWindowTitle(f"Image Marker (v{_VERSION}) — {self._current_name}")
        if self._btn_mark_rect.isChecked():
            hint = "  |  Click first corner of rectangle…"
        elif self._btn_mark_line.isChecked():
            hint = "  |  Click to place line marker"
        else:
            hint = ""
        self._status.setText(
            f"{self._current_name}  ({self._image_width} × {self._image_height} px){hint}"
        )

    def _on_mark_line_toggled(self, enabled: bool) -> None:
        if enabled and self._btn_mark_rect.isChecked():
            self._btn_mark_rect.setChecked(False)
        self._canvas.set_line_mode(enabled)
        if enabled:
            self._status.setText("Click to place line marker")

    def _on_perimeter_toggled(self, enabled: bool) -> None:
        self._canvas.set_perimeter(enabled)
        self._laser.set_perimeter(enabled)

    def _on_logo_toggled(self, enabled: bool) -> None:
        if enabled and not self._laser.logo_loaded:
            path, _ = QFileDialog.getOpenFileName(
                self, "Open Logo SVG", str(Path.home()), "SVG Files (*.svg);;All Files (*)"
            )
            if not path or not self._laser.load_logo(path):
                self._btn_logo.setChecked(False)
                return
        self._laser.set_logo_mode(enabled)

    def _on_mark_rect_toggled(self, enabled: bool) -> None:
        if enabled and self._btn_mark_line.isChecked():
            self._btn_mark_line.setChecked(False)
        self._canvas.set_rect_mode(enabled)
        if enabled:
            self._status.setText("Click first corner of rectangle, then second corner")

    def _on_rect_changed(self, rect) -> None:
        if rect is None:
            self._laser.clear_rect()
            if self._btn_mark_rect.isChecked():
                self._status.setText("Click first corner of rectangle, then second corner")
            return
        x0, y0, x1, y1 = rect
        lx0 = int(x0 / self._image_width * 4095)
        ly0 = 4095 - int(y0 / self._image_height * 4095)
        lx1 = int(x1 / self._image_width * 4095)
        ly1 = 4095 - int(y1 / self._image_height * 4095)
        self._laser.set_rect(lx0, ly0, lx1, ly1)
        self._status.setText(
            f"Rectangle: ({x0:.0f}, {y0:.0f}) → ({x1:.0f}, {y1:.0f}) px"
        )

    def _on_marker_changed(self, image_x: float | None):
        if image_x is not None and self._image_width > 0:
            self._laser.set_marker(image_x / self._image_width)
        else:
            self._laser.set_marker(None)

        if not self._current_name:
            return
        w, h = self._canvas.image_size
        base = f"{self._current_name}  ({w} × {h} px)  |  "
        if image_x is None:
            if self._btn_mark_line.isChecked():
                self._status.setText(base + "Click to place line marker")
        else:
            self._status.setText(base + f"Marker: x = {image_x:.1f} px")
