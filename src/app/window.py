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
"""

_LASER_CONNECTED_STYLE = "color: #00cc44; font-size: 12px;"
_LASER_DISCONNECTED_STYLE = "color: #888; font-size: 12px;"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Image Marker")
        self.resize(1000, 750)

        self._current_name: str = ""
        self._image_width: int = 1

        # --- Laser DAC ---
        self._laser = HeliosOutput()
        laser_ok = self._laser.open()

        # --- Button bar ---
        self._btn_open = QPushButton("Open Image")
        self._btn_open.setStyleSheet(_BUTTON_STYLE)
        self._btn_open.clicked.connect(self._open_image)

        self._btn_clear = QPushButton("Clear Marker")
        self._btn_clear.setStyleSheet(_BUTTON_STYLE)
        self._btn_clear.setEnabled(False)
        self._btn_clear.clicked.connect(self._on_clear_clicked)

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
        btn_bar.addWidget(self._btn_clear)
        btn_bar.addStretch()
        btn_bar.addWidget(self._laser_label)

        # --- Canvas ---
        self._canvas = ImageCanvas()
        self._canvas.marker_changed.connect(self._on_marker_changed)

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

        self._build_menu()

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

        clear_act = QAction("&Clear Marker", self)
        clear_act.setShortcut("Ctrl+D")
        clear_act.triggered.connect(self._on_clear_clicked)
        file_menu.addAction(clear_act)

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

        self._current_name = Path(path).name
        self._image_width, h = self._canvas.image_size
        self.setWindowTitle(f"Image Marker — {self._current_name}")
        self._status.setText(
            f"{self._current_name}  ({self._image_width} × {h} px)"
            "  |  Left-click to place marker · Right-click to clear"
        )

    def _on_clear_clicked(self):
        self._canvas.clear_marker()

    def _on_marker_changed(self, image_x: float | None):
        self._btn_clear.setEnabled(image_x is not None)

        # Update laser output
        if image_x is not None and self._image_width > 0:
            self._laser.set_marker(image_x / self._image_width)
        else:
            self._laser.set_marker(None)

        if not self._current_name:
            return
        w, h = self._canvas.image_size
        base = f"{self._current_name}  ({w} × {h} px)  |  "
        if image_x is None:
            self._status.setText(base + "Left-click to place marker · Right-click to clear")
        else:
            self._status.setText(base + f"Marker: x = {image_x:.1f} px")
