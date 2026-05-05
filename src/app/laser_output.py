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
_LOGO_PPS = 25_000

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
        self._logo_pts: list | None = None
        self._logo_mode: bool = False
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

    @property
    def logo_loaded(self) -> bool:
        return self._logo_pts is not None

    def load_logo(self, path: str) -> bool:
        """Parse an SVG file and pre-compute laser scan points. Returns True on success."""
        try:
            pts = self._svg_to_scan_points(path)
        except Exception:
            return False
        if not pts:
            return False
        with self._lock:
            self._logo_pts = pts
        return True

    def set_logo_mode(self, enabled: bool) -> None:
        with self._lock:
            self._logo_mode = enabled

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
          0. Blank head-dwell at (0, 0) — combined with tail of previous frame
             this gives galvos ~1.3 ms to settle at the corner before the
             perimeter turns on, eliminating the lower-left corner dip.
          1. Perimeter — clockwise, dashed, ends at (0, 0)
          2. Blank transition (0,0) → (helios_x, 0)
          3. Vertical line — fully lit, ends at (helios_x, 4095)
          4. Blank dwell at (helios_x, 4095) — laser off but galvos stationary,
             allows laser to fully extinguish before galvos start moving
          5. Blank tail (helios_x, 4095) → (0, 0) so next frame starts dark
        """
        pts: list[tuple[int, int, bool]] = []

        # 0. Blank head-dwell at (0, 0) — galvo settle before perimeter starts
        for _ in range(20):
            pts.append((0, 0, False))

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
    def _svg_to_scan_points(svg_path: str) -> list:
        """Parse an SVG file and return (lx, ly, lit) tuples in 0-4095 laser space."""
        import re
        import xml.etree.ElementTree as ET
        from svgpathtools import parse_path

        tree = ET.parse(svg_path)
        root = tree.getroot()

        ns_prefix = root.tag.split('}')[0] + '}' if root.tag.startswith('{') else ''

        def strip_ns(tag):
            return tag[len(ns_prefix):] if ns_prefix and tag.startswith(ns_prefix) else tag

        vb = [float(v) for v in root.get('viewBox', '0 0 800 800').split()]
        vb_x, vb_y, vb_w, vb_h = vb

        def parse_transform(s):
            s = s.strip()
            m = re.match(r'matrix\(([^)]+)\)', s)
            if m:
                v = [float(x) for x in re.split(r'[\s,]+', m.group(1).strip())]
                return tuple(v[:6])
            m = re.match(r'translate\(([^)]+)\)', s)
            if m:
                v = [float(x) for x in re.split(r'[\s,]+', m.group(1).strip())]
                return (1.0, 0.0, 0.0, 1.0, v[0], v[1] if len(v) > 1 else 0.0)
            m = re.match(r'scale\(([^)]+)\)', s)
            if m:
                v = [float(x) for x in re.split(r'[\s,]+', m.group(1).strip())]
                sx = v[0]; sy = v[1] if len(v) > 1 else v[0]
                return (sx, 0.0, 0.0, sy, 0.0, 0.0)
            return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

        def compose(outer, inner):
            # outer applied after inner: world_pt = outer(inner(local_pt))
            a1, b1, c1, d1, e1, f1 = outer
            a2, b2, c2, d2, e2, f2 = inner
            return (a1*a2+c1*b2, b1*a2+d1*b2, a1*c2+c1*d2,
                    b1*c2+d1*d2, a1*e2+c1*f2+e1, b1*e2+d1*f2+f1)

        def apply_T(T, cx, cy):
            a, b, c, d, e, f = T
            return a*cx + c*cy + e, b*cx + d*cy + f

        def to_laser(x, y):
            lx = int((x - vb_x) / vb_w * 4095)
            ly = 4095 - int((y - vb_y) / vb_h * 4095)
            return max(0, min(4095, lx)), max(0, min(4095, ly))

        identity = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        path_list: list[tuple[str, tuple]] = []

        def collect(elem, parent_T):
            t_str = elem.get('transform', '')
            T = compose(parent_T, parse_transform(t_str)) if t_str else parent_T
            if strip_ns(elem.tag) == 'path':
                d = elem.get('d', '')
                if d:
                    path_list.append((d, T))
            for child in elem:
                collect(child, T)

        collect(root, identity)

        STEP = 3.0      # SVG units between laser sample points
        BLANK_DWELL = 8  # dark points for galvo settle at sub-path starts

        result: list[tuple[int, int, bool]] = []

        for d_str, T in path_list:
            try:
                path = parse_path(d_str)
            except Exception:
                continue
            segs = list(path)
            if not segs:
                continue

            prev_end = None
            for seg in segs:
                seg_start = seg.point(0)
                is_new = (prev_end is None) or (abs(seg_start - prev_end) > 1e-6)
                if is_new:
                    sx, sy = apply_T(T, seg_start.real, seg_start.imag)
                    lx0, ly0 = to_laser(sx, sy)
                    for _ in range(BLANK_DWELL):
                        result.append((lx0, ly0, False))

                seg_len = seg.length()
                n = max(2, int(seg_len / STEP) + 1)
                for k in range(n):
                    p = seg.point(k / (n - 1))
                    px, py = apply_T(T, p.real, p.imag)
                    lx, ly = to_laser(px, py)
                    result.append((lx, ly, True))

                prev_end = seg.point(1)

        return result

    @staticmethod
    def _build_logo_frame(logo_pts: list) -> tuple[ctypes.Array, int]:
        pts: list[tuple[int, int, bool]] = []
        for _ in range(20):
            pts.append((0, 0, False))
        pts.extend(logo_pts)
        if logo_pts:
            lx, ly, _ = logo_pts[-1]
            for _ in range(10):
                pts.append((lx, ly, False))
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
                logo_mode = self._logo_mode
                logo_pts = self._logo_pts

            if logo_mode and logo_pts is not None:
                frame, n = self._build_logo_frame(logo_pts)
                pps = _LOGO_PPS
            elif nx is not None:
                helios_x = int(nx * 4095)
                if show_perimeter:
                    frame, n = self._build_combined_frame(helios_x)
                else:
                    frame, n = self._build_line_frame(helios_x)
                pps = _LINE_PPS
            else:
                frame, n = self._blank_frame()
                pps = _LINE_PPS

            ptr = ctypes.cast(frame, POINTER(HeliosPoint))
            for dac in range(self._dac_count):
                # Poll until DAC buffer is ready (up to ~100 ms)
                for _ in range(100):
                    if not self._running:
                        return
                    if self._lib.GetStatus(dac) == _HELIOS_READY:
                        break
                    time.sleep(0.001)
                self._lib.WriteFrame(dac, pps, _FLAG_SINGLE_MODE, ptr, n)
