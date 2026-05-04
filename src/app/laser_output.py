"""Helios USB DAC laser output — projects a vertical line at the marker position."""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import POINTER, Structure, c_int, c_uint, c_uint8, c_uint16
from pathlib import Path

# Libraries are stored in the project root (two levels above src/app/)
_LIB_DIR = Path(__file__).parent.parent.parent

# Vertical line
_LINE_POINTS = 200

# Dashed perimeter: points per side and dash on/off lengths (in points)
_PERIM_PTS_PER_SIDE = 120
_DASH_ON = 15
_DASH_OFF = 10

_LINE_PPS = 30_000

# WriteFrame flag: play once per call (don't loop internally)
_FLAG_SINGLE_MODE = 1 << 1

_HELIOS_READY = 1


class HeliosPoint(Structure):
    # _pack_=1 is required — without it ctypes adds padding and corrupts the frame
    _pack_ = 1
    _fields_ = [
        ("x", c_uint16),  # 0-4095
        ("y", c_uint16),  # 0-4095
        ("r", c_uint8),
        ("g", c_uint8),
        ("b", c_uint8),
        ("i", c_uint8),   # intensity
    ]


class HeliosOutput:
    """Drives all connected Helios USB DACs to project a vertical green line."""

    def __init__(self):
        self._lib = None
        self._dac_count = 0
        self._normalized_x: float | None = None  # 0.0-1.0, or None to blank
        self._show_perimeter: bool = True
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._lib is not None and self._dac_count > 0

    @property
    def device_count(self) -> int:
        return self._dac_count

    def open(self) -> bool:
        """Load the Helios library and open all connected devices. Returns True on success."""
        lib = self._load_library()
        if lib is None:
            return False
        count = lib.OpenDevices()
        if count <= 0:
            lib.CloseDevices()
            return False
        self._lib = lib
        self._dac_count = count
        self._running = True
        self._thread = threading.Thread(target=self._output_loop, daemon=True)
        self._thread.start()
        return True

    def close(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        if self._lib:
            self._lib.CloseDevices()
            self._lib = None
        self._dac_count = 0

    def set_perimeter(self, enabled: bool) -> None:
        with self._lock:
            self._show_perimeter = enabled

    def set_marker(self, normalized_x: float | None) -> None:
        """Update the projected line position.

        normalized_x: marker x as a fraction of image width (0.0-1.0),
                      or None to blank the laser output.
        """
        with self._lock:
            self._normalized_x = normalized_x

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_library():
        names = {
            "darwin": ["libHeliosDacAPI.dylib"],
            "linux":  ["libHeliosDacAPI.so", "libHeliosDacAPI.so.1"],
            "win32":  ["HeliosDacAPI.dll"],
        }.get(sys.platform, [])

        # Build candidate paths: project root first, then bare name (system path)
        candidates = [str(_LIB_DIR / n) for n in names] + names

        for name in candidates:
            try:
                lib = ctypes.CDLL(name)
                lib.OpenDevices.argtypes = []
                lib.OpenDevices.restype = c_int
                lib.CloseDevices.argtypes = []
                lib.CloseDevices.restype = None
                lib.GetStatus.argtypes = [c_uint]
                lib.GetStatus.restype = c_int
                lib.WriteFrame.argtypes = [c_uint, c_uint, c_uint8, POINTER(HeliosPoint), c_uint]
                lib.WriteFrame.restype = c_int
                return lib
            except OSError:
                continue
        return None

    @staticmethod
    def _build_line_frame(helios_x: int) -> tuple[ctypes.Array, int]:
        """Vertical green line only."""
        n = _LINE_POINTS
        frame = (HeliosPoint * n)()
        for i in range(n):
            frame[i].x = helios_x
            frame[i].y = int(i * 4095 / (n - 1))
            frame[i].g = 255
            frame[i].i = 255
        return frame, n

    @staticmethod
    def _build_combined_frame(helios_x: int) -> tuple[ctypes.Array, int]:
        """Dashed perimeter rectangle + vertical green line at helios_x.

        Frame structure (all transitions are blanked so galvos move dark):
          1. Perimeter — clockwise, dashed, ends at (0, 0)
          2. Blank transition (0,0) → (helios_x, 0)
          3. Vertical line — fully lit, ends at (helios_x, 4095)
          4. Blank dwell at (helios_x, 4095) — laser off but galvos stationary,
             allows laser to fully extinguish before galvos start moving
          5. Blank tail (helios_x, 4095) → (0, 0) so next frame starts dark
        """
        pts: list[tuple[int, int, bool]] = []

        # 1. Dashed perimeter — clockwise from bottom-left, ends at (0, 0)
        n = _PERIM_PTS_PER_SIDE
        cycle = _DASH_ON + _DASH_OFF
        sides = [
            [(int(i * 4095 / (n - 1)), 0)         for i in range(n)],  # bottom L→R
            [(4095, int(i * 4095 / (n - 1)))       for i in range(n)],  # right  B→T
            [(int((n-1-i) * 4095 / (n-1)), 4095)  for i in range(n)],  # top    R→L
            [(0, int((n-1-i) * 4095 / (n-1)))     for i in range(n)],  # left   T→B
        ]
        for side in sides:
            for j, (x, y) in enumerate(side):
                pts.append((x, y, (j % cycle) < _DASH_ON))

        # 2. Blank transition: (0, 0) → (helios_x, 0)
        for _ in range(20):
            pts.append((helios_x, 0, False))

        # 3. Vertical line: (helios_x, 0) → (helios_x, 4095)
        m = _LINE_POINTS
        for i in range(m):
            pts.append((helios_x, int(i * 4095 / (m - 1)), True))

        # 4. Blank dwell at top of line — laser off, galvos stationary
        for _ in range(10):
            pts.append((helios_x, 4095, False))

        # 5. Blank tail: (helios_x, 4095) → (0, 0) so next frame starts dark
        for _ in range(20):
            pts.append((0, 0, False))

        total = len(pts)
        frame = (HeliosPoint * total)()
        for i, (x, y, lit) in enumerate(pts):
            frame[i].x = x
            frame[i].y = y
            frame[i].g = 255 if lit else 0
            frame[i].i = 255 if lit else 0
        return frame, total

    @staticmethod
    def _blank_frame() -> tuple[ctypes.Array, int]:
        """Single dark point at centre — parks the galvos with the laser off."""
        frame = (HeliosPoint * 1)()
        frame[0].x = 2048
        frame[0].y = 2048
        return frame, 1

    def _output_loop(self) -> None:
        while self._running:
            with self._lock:
                nx = self._normalized_x
                show_perimeter = self._show_perimeter

            if nx is not None:
                helios_x = int(nx * 4095)
                if show_perimeter:
                    frame, n = self._build_combined_frame(helios_x)
                else:
                    frame, n = self._build_line_frame(helios_x)
            else:
                frame, n = self._blank_frame()

            ptr = ctypes.cast(frame, POINTER(HeliosPoint))
            for dac in range(self._dac_count):
                # Poll until DAC buffer is ready (up to ~100 ms)
                for _ in range(100):
                    if not self._running:
                        return
                    if self._lib.GetStatus(dac) == _HELIOS_READY:
                        break
                    time.sleep(0.001)
                self._lib.WriteFrame(dac, _LINE_PPS, _FLAG_SINGLE_MODE, ptr, n)
