"""Entry point for the Image Marker application."""

import sys

from PyQt6.QtWidgets import QApplication

from .window import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Image Marker")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
