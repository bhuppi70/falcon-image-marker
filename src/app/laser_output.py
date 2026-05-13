"""Helios USB DAC laser output — projects a vertical line at the marker position."""

from __future__ import annotations

import ctypes
import math
import sys
import threading
import time
from ctypes import POINTER, Structure, c_int, c_uint, c_uint8, c_uint16
from pathlib import Path

# Libraries are stored in the project root (two levels above src/app/)
_LIB_DIR = Path(__file__).parent.parent.parent

# Perimeter: points per side
_PERIM_PTS_PER_SIDE = 120

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
        self._line_pts: tuple[int, int, int, int] | None = None  # laser coords, or None to blank
        self._first_pt: tuple[int, int] | None = None            # single point while awaiting p2
        self._show_perimeter: bool = False
        self._logo_pts: list | None = None
        self._logo_mode: bool = False
        self._rect_coords: tuple[int, int, int, int] | None = None
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

    def set_rect(self, lx0: int, ly0: int, lx1: int, ly1: int) -> None:
        with self._lock:
            self._rect_coords = (lx0, ly0, lx1, ly1)

    def clear_rect(self) -> None:
        with self._lock:
            self._rect_coords = None

    def set_line(self, lx0: int, ly0: int, lx1: int, ly1: int) -> None:
        with self._lock:
            self._line_pts = (lx0, ly0, lx1, ly1)

    def clear_line(self) -> None:
        with self._lock:
            self._line_pts = None

    def set_first_point(self, lx: int, ly: int) -> None:
        with self._lock:
            self._first_pt = (lx, ly)

    def clear_first_point(self) -> None:
        with self._lock:
            self._first_pt = None

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
    def _build_line_frame(lx0: int, ly0: int, lx1: int, ly1: int) -> tuple[ctypes.Array, int]:
        """Arbitrary green line — triangle scan (A→B→A), fully lit, seamless frame wrap.

        The frame starts and ends at (lx0, ly0), so there is zero galvo travel
        between frames and no blanked return stroke.
        """
        density = _PERIM_PTS_PER_SIDE / 4095
        n = max(2, round(math.hypot(lx1 - lx0, ly1 - ly0) * density))
        _DWELL = 8  # blank points at turnaround so galvo decelerates before reversing
        total = 2 * n + _DWELL
        frame = (HeliosPoint * total)()
        for i in range(n):
            t = i / (n - 1)
            frame[i].x = round(lx0 + t * (lx1 - lx0))
            frame[i].y = round(ly0 + t * (ly1 - ly0))
            frame[i].g = 255
            frame[i].i = 255
        for i in range(_DWELL):
            frame[n + i].x = lx1
            frame[n + i].y = ly1
        for i in range(n):
            t = i / (n - 1)
            frame[n + _DWELL + i].x = round(lx1 + t * (lx0 - lx1))
            frame[n + _DWELL + i].y = round(ly1 + t * (ly0 - ly1))
            frame[n + _DWELL + i].g = 255
            frame[n + _DWELL + i].i = 255
        return frame, total

    @staticmethod
    def _build_rect_frame(lx0: int, ly0: int, lx1: int, ly1: int) -> tuple[ctypes.Array, int]:
        """Solid rectangle loop — all four sides fully lit, seamless frame wrap at (lx0, ly0).

        Point density matches the full-screen perimeter (~34 laser units between points),
        so smaller rectangles automatically get fewer points and higher frame rates.
        The frame starts and ends at (lx0, ly0); the galvo never returns to origin.
        """
        density = _PERIM_PTS_PER_SIDE / 4095  # pts per laser unit
        n_h = max(2, round(abs(lx1 - lx0) * density))
        n_v = max(2, round(abs(ly1 - ly0) * density))

        pts: list[tuple[int, int]] = []
        # Side 1: (lx0, ly0) → (lx1, ly0)
        for i in range(n_h):
            pts.append((lx0 + round(i * (lx1 - lx0) / (n_h - 1)), ly0))
        # Side 2: (lx1, ly0) → (lx1, ly1)
        for i in range(n_v):
            pts.append((lx1, ly0 + round(i * (ly1 - ly0) / (n_v - 1))))
        # Side 3: (lx1, ly1) → (lx0, ly1)
        for i in range(n_h):
            pts.append((lx1 + round(i * (lx0 - lx1) / (n_h - 1)), ly1))
        # Side 4: (lx0, ly1) → (lx0, ly0)  — ends at start, seamless loop
        for i in range(n_v):
            pts.append((lx0, ly1 + round(i * (ly0 - ly1) / (n_v - 1))))

        total = len(pts)
        frame = (HeliosPoint * total)()
        for i, (x, y) in enumerate(pts):
            frame[i].x = x
            frame[i].y = y
            frame[i].g = 255
            frame[i].i = 255
        return frame, total

    @staticmethod
    def _build_combined_frame(lx0: int, ly0: int, lx1: int, ly1: int) -> tuple[ctypes.Array, int]:
        """Dashed perimeter rectangle + arbitrary green line (triangle scan).

        Frame structure (all transitions blanked so galvos move dark):
          0. Blank head-dwell at (0, 0)
          1. Perimeter — clockwise, dashed, ends at (0, 0)
          2. Blank transition (0, 0) → (lx0, ly0)
          3. Line triangle scan A→B→A, fully lit, ends at (lx0, ly0)
          4. Blank tail (lx0, ly0) → (0, 0) — seamless into next frame's head dwell
        """
        pts: list[tuple[int, int, bool]] = []

        # 0. Blank head-dwell at (0, 0) — galvo settle before perimeter starts
        for _ in range(20):
            pts.append((0, 0, False))

        # 1. Solid perimeter — clockwise from bottom-left, ends at (0, 0)
        n = _PERIM_PTS_PER_SIDE
        sides = [
            [(int(i * 4095 / (n - 1)), 0)         for i in range(n)],  # bottom L→R
            [(4095, int(i * 4095 / (n - 1)))       for i in range(n)],  # right  B→T
            [(int((n-1-i) * 4095 / (n-1)), 4095)  for i in range(n)],  # top    R→L
            [(0, int((n-1-i) * 4095 / (n-1)))     for i in range(n)],  # left   T→B
        ]
        for side in sides:
            for x, y in side:
                pts.append((x, y, True))

        # 2a. Blank dwell at (0, 0) — galvo decelerates from the left-side perimeter scan
        #     (which ends at full downward speed) before reversing to jump to A.
        for _ in range(20):
            pts.append((0, 0, False))
        # 2b. Blank settle at A — for lines near the top of the field the jump from
        #     (0,0) to A can exceed 3000 laser units; 60 pts (2 ms) gives enough
        #     settle time before the laser turns on for the forward scan.
        for _ in range(60):
            pts.append((lx0, ly0, False))

        # 3. Triangle scan: A→B then B→A, all lit, ends at (lx0, ly0)
        density = _PERIM_PTS_PER_SIDE / 4095
        m = max(2, round(math.hypot(lx1 - lx0, ly1 - ly0) * density))
        for i in range(m):
            t = i / (m - 1)
            pts.append((round(lx0 + t * (lx1 - lx0)), round(ly0 + t * (ly1 - ly0)), True))
        # Blank dwell at B: galvo decelerates before reversing (prevents tail at lower end).
        for _ in range(8):
            pts.append((lx1, ly1, False))
        for i in range(m):
            t = i / (m - 1)
            pts.append((round(lx1 + t * (lx0 - lx1)), round(ly1 + t * (ly0 - ly1)), True))
        # Blank dwell at A: galvo decelerates before jumping to origin (prevents tail at upper end).
        for _ in range(8):
            pts.append((lx0, ly0, False))

        # 4. Blank tail: line start → (0, 0)
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
        import math
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

        # Elements whose subtree contains definition/reference paths, not rendered geometry.
        _SKIP_TAGS = {'defs', 'marker', 'pattern', 'symbol', 'clipPath', 'mask', 'filter',
                      'namedview', 'metadata', 'sodipodi:namedview'}

        def collect(elem, parent_T):
            tag = strip_ns(elem.tag)
            if tag in _SKIP_TAGS:
                return  # skip entire subtree — these are definitions, not drawn paths
            t_str = elem.get('transform', '')
            T = compose(parent_T, parse_transform(t_str)) if t_str else parent_T
            if tag == 'path':
                d = elem.get('d', '')
                if d:
                    path_list.append((d, T))
            for child in elem:
                collect(child, T)

        collect(root, identity)

        # STEP: SVG units between laser sample points.  1.0 gives smooth bezier curves;
        # scale relative to viewBox so both small (Bigfoot, 211 units) and large (800 units)
        # SVGs sample at a similar physical density (~20 laser units between points).
        STEP = max(0.5, vb_w * 20 / 4095)
        BLANK_DWELL = 8   # dark-dwell points when galvos must jump to a new position
        # Two endpoints are "continuous" when they map to the same laser pixel (≤ 10 units).
        # This detects connected strokes split across separate <path> elements (e.g. Bigfoot)
        # without accidentally bridging genuinely separate strokes.
        CONTINUITY_THRESH = 10

        result: list[tuple[int, int, bool]] = []
        last_lx: int | None = None  # laser coords of last drawn endpoint, tracked across paths
        last_ly: int | None = None

        for d_str, T in path_list:
            try:
                path = parse_path(d_str)
            except Exception:
                continue
            segs = list(path)
            if not segs:
                continue

            # Effective scale: magnitude of the x-axis vector after applying T.
            # seg.length() is in local (pre-transform) coordinates; multiply by t_scale
            # to get SVG document-space length before comparing to STEP.
            t_scale = math.hypot(T[0], T[1])

            prev_end_raw = None  # complex endpoint of last seg (for intra-path sub-path detection)
            for seg in segs:
                seg_start = seg.point(0)

                # Compute this segment's start in laser space
                sx, sy = apply_T(T, seg_start.real, seg_start.imag)
                lx0, ly0 = to_laser(sx, sy)

                seg_len = seg.length()
                if seg_len < 0.01:
                    # Degenerate/zero-length segment: update tracking state but emit
                    # NO points — not even a blank dwell.  This prevents isolated
                    # zero-length artefact paths (common in CAD-exported SVGs) from
                    # forcing unnecessary galvo jumps and leaving logo_pts[-1] at an
                    # unrelated position that causes a long blanked slew on frame wrap.
                    prev_end_raw = seg.point(1)
                    last_lx, last_ly = lx0, ly0
                    continue

                # Blank-dwell only when galvos must actually jump (non-degenerate segs only).
                # Three cases that constitute a genuine gap:
                #   1. Very first point (no prior position)
                #   2. Intra-path sub-path: M command gap in path coordinates
                #   3. Cross-path or large position jump in laser coordinates
                is_gap = (
                    last_lx is None
                    or (prev_end_raw is not None and abs(seg_start - prev_end_raw) > 1e-6)
                    or abs(lx0 - last_lx) + abs(ly0 - last_ly) > CONTINUITY_THRESH
                )
                if is_gap:
                    for _ in range(BLANK_DWELL):
                        result.append((lx0, ly0, False))

                n = max(2, int(seg_len * t_scale / STEP) + 1)
                for k in range(n):
                    p = seg.point(k / (n - 1))
                    px, py = apply_T(T, p.real, p.imag)
                    lx, ly = to_laser(px, py)
                    result.append((lx, ly, True))

                prev_end_raw = seg.point(1)
                ex, ey = apply_T(T, prev_end_raw.real, prev_end_raw.imag)
                last_lx, last_ly = to_laser(ex, ey)

        return result

    @staticmethod
    def _build_logo_frame(logo_pts: list) -> tuple[ctypes.Array, int]:
        # Tail returns to the first content position (not to 0,0) so galvos stay
        # near the logo between frames and don't create a corner artifact.
        pts: list[tuple[int, int, bool]] = list(logo_pts)
        if logo_pts:
            lx0, ly0, _ = logo_pts[0]   # first content position
            lx_end, ly_end, _ = logo_pts[-1]
            for _ in range(10):          # dwell at last lit position so laser extinguishes
                pts.append((lx_end, ly_end, False))
            for _ in range(20):          # slew back to first position, dark
                pts.append((lx0, ly0, False))
        total = len(pts)
        frame = (HeliosPoint * total)()
        for i, (x, y, lit) in enumerate(pts):
            frame[i].x = x
            frame[i].y = y
            frame[i].g = 255 if lit else 0
            frame[i].i = 255 if lit else 0
        return frame, total

    @staticmethod
    def _build_point_frame(lx: int, ly: int) -> tuple[ctypes.Array, int]:
        """Static lit point — galvo parks at (lx, ly) with laser on.

        900 identical points at 30 kPPS = 30 ms frame duration.  The rewrite
        overhead (polling + USB write) is ~1-2 ms, giving ~97% duty cycle.
        A single-point frame would play in 33 µs and sit dark for ~1.5 ms
        between rewrites — only ~2% duty cycle.
        """
        n = 900
        frame = (HeliosPoint * n)()
        for i in range(n):
            frame[i].x = lx
            frame[i].y = ly
            frame[i].g = 255
            frame[i].i = 255
        return frame, n

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
                line_pts = self._line_pts
                first_pt = self._first_pt
                show_perimeter = self._show_perimeter
                logo_mode = self._logo_mode
                logo_pts = self._logo_pts
                rect_coords = self._rect_coords

            if logo_mode and logo_pts is not None:
                frame, n = self._build_logo_frame(logo_pts)
                pps = _LOGO_PPS
            elif rect_coords is not None:
                frame, n = self._build_rect_frame(*rect_coords)
                pps = _LINE_PPS
            elif line_pts is not None:
                if show_perimeter:
                    frame, n = self._build_combined_frame(*line_pts)
                else:
                    frame, n = self._build_line_frame(*line_pts)
                pps = _LINE_PPS
            elif first_pt is not None:
                frame, n = self._build_point_frame(*first_pt)
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
