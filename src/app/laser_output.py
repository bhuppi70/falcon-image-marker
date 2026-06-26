"""Helios USB DAC laser output — projects a vertical line at the marker position."""

from __future__ import annotations

import ctypes
import math
import sys
import threading
import time
from ctypes import POINTER, Structure, c_int, c_uint, c_uint8, c_uint16
from pathlib import Path

# In a PyInstaller frozen bundle everything is extracted to sys._MEIPASS;
# in normal development the dylibs live at the project root (three levels up).
_LIB_DIR = (Path(sys._MEIPASS) if getattr(sys, 'frozen', False)
            else Path(__file__).parent.parent.parent)

# Perimeter: points per side
_PERIM_PTS_PER_SIDE = 120

_LINE_PPS = 30_000
_LOGO_PPS = 25_000

_RECT_CORNER_DWELL = 8   # lit points at each corner so the galvo decelerates before turning

# WriteFrame flag: play once per call (don't loop internally)
_FLAG_SINGLE_MODE = 1 << 1

_HELIOS_READY = 1

# X mirror is first in the beam path, Y mirror second.  This means:
#   x_surf = D·tan(θx) / cos(θy)  — vertical edges bow inward without correction
#   y_surf = D·tan(θy)             — horizontal edges are straight, no correction needed
# _SCAN_FIELD_HALFANGLE_DEG is the physical half-angle of the scan field; only
# _v_edge_pts uses it to correct the inward bow on left and right sides.
_SCAN_FIELD_HALFANGLE_DEG = 15.8
_SCAN_FIELD_HALFANGLE_RAD = math.radians(_SCAN_FIELD_HALFANGLE_DEG)


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


def _h_edge_pts(lx0: int, lx1: int, ly: int, n: int) -> list[tuple[int, int]]:
    """Horizontal edge — linear interpolation, no pincushion correction.

    With X mirror first in the beam path, y_surf = D·tan(θy) which is independent
    of θx.  Constant ly already traces a straight horizontal line on the projection
    surface, so no correction is needed.
    """
    return [(round(lx0 + i * (lx1 - lx0) / (n - 1)), ly) for i in range(n)]


def _v_edge_pts(lx: int, ly0: int, ly1: int, n: int) -> list[tuple[int, int]]:
    """Vertical edge with pincushion correction.

    With X mirror first, x_surf = D·tan(θx)/cos(θy), so a constant-lx scan bows
    inward.  Each point is shifted so the projected x remains constant:
      θx_c = atan(tan(θx_nom) · cos(θy))
    """
    half_angle = _SCAN_FIELD_HALFANGLE_RAD
    tan_vx = math.tan((lx - 2048) / 2048 * half_angle)
    pts = []
    for i in range(n):
        ly = round(ly0 + i * (ly1 - ly0) / (n - 1))
        theta_y = (ly - 2048) / 2048 * half_angle
        lx_c = max(0, min(4095, round(2048 + math.atan(tan_vx * math.cos(theta_y)) / half_angle * 2048)))
        pts.append((lx_c, ly))
    return pts


def _v_corrected_x(lx: int, ly: int) -> int:
    """Corrected DAC x for a point that should lie on the vertical column lx at height ly."""
    half_angle = _SCAN_FIELD_HALFANGLE_RAD
    tan_vx = math.tan((lx - 2048) / 2048 * half_angle)
    theta_y = (ly - 2048) / 2048 * half_angle
    return max(0, min(4095, round(2048 + math.atan(tan_vx * math.cos(theta_y)) / half_angle * 2048)))


def _corrected_line_pts(lx0: int, ly0: int, lx1: int, ly1: int, n: int) -> list[tuple[int, int]]:
    """DAC points for a line that appears straight on the projection surface.

    DAC-space linear interpolation bows near field edges because
    x_surf = D·tan(θx)/cos(θy).  This function interpolates linearly in screen
    space (ux = tan(θx)/cos(θy), uy = tan(θy)) and inverts back to DAC.
    """
    half_angle = _SCAN_FIELD_HALFANGLE_RAD

    def to_screen(lx: int, ly: int) -> tuple[float, float]:
        theta_x = (lx - 2048) / 2048 * half_angle
        theta_y = (ly - 2048) / 2048 * half_angle
        # Use tan(θx) — not tan(θx)/cos(θy) — so the x basis matches _v_edge_pts.
        # A constant-lx (vertical) DAC line maps to constant ux and inverts to the
        # same corrected path as _v_edge_pts.  Error for diagonal lines is negligible.
        return math.tan(theta_x), math.tan(theta_y)

    def from_screen(ux: float, uy: float) -> tuple[int, int]:
        theta_y = math.atan(uy)
        theta_x = math.atan(ux * math.cos(theta_y))
        return (max(0, min(4095, round(2048 + theta_x / half_angle * 2048))),
                max(0, min(4095, round(2048 + theta_y / half_angle * 2048))))

    ux0, uy0 = to_screen(lx0, ly0)
    ux1, uy1 = to_screen(lx1, ly1)
    return [from_screen(ux0 + i / (n - 1) * (ux1 - ux0),
                        uy0 + i / (n - 1) * (uy1 - uy0)) for i in range(n)]


class HeliosOutput:
    """Drives all connected Helios USB DACs to project a vertical green line."""

    def __init__(self):
        self._lib = None
        self._dac_count = 0
        self._line_pts: tuple[int, int, int, int] | None = None  # laser coords, or None to blank
        self._first_pt: tuple[int, int] | None = None            # single point while awaiting p2
        self._show_perimeter: bool = False
        self._bf_logo_pts: list | None = None
        self._bf_logo_mode: bool = False
        self._ae_logo_pts: list | None = None
        self._ae_logo_mode: bool = False
        self._rect_coords: tuple[int, int, int, int] | None = None
        self._neutral_beam: bool = False
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
    def bf_logo_loaded(self) -> bool:
        return self._bf_logo_pts is not None

    @property
    def ae_logo_loaded(self) -> bool:
        return self._ae_logo_pts is not None

    @property
    def ae_logo_pts(self) -> list | None:
        with self._lock:
            return list(self._ae_logo_pts) if self._ae_logo_pts else None

    @property
    def bf_logo_pts(self) -> list | None:
        with self._lock:
            return list(self._bf_logo_pts) if self._bf_logo_pts else None

    @staticmethod
    def export_scan_diagram(pts: list, out_path: str) -> None:
        """Render a scan-path diagram to a PNG file.

        Green lines = laser ON, grey lines = laser OFF (blank slew).
        A yellow circle marks the start position; red marks the end.
        """
        from PIL import Image, ImageDraw

        W, H = 900, 900
        PAD = 40
        IMG_SZ = W - 2 * PAD

        img = Image.new('RGB', (W, H), (18, 18, 18))
        draw = ImageDraw.Draw(img)

        def to_px(lx, ly):
            x = PAD + int(lx / 4095 * IMG_SZ)
            y = PAD + int((4095 - ly) / 4095 * IMG_SZ)
            return x, y

        for v in range(0, 4096, 1024):
            x = PAD + int(v / 4095 * IMG_SZ)
            y = PAD + int(v / 4095 * IMG_SZ)
            draw.line([(x, PAD), (x, H - PAD)], fill=(40, 40, 40), width=1)
            draw.line([(PAD, y), (W - PAD, y)], fill=(40, 40, 40), width=1)

        COLOR_ON  = (0, 220, 60)
        COLOR_OFF = (80, 80, 80)

        prev_px = None
        for lx, ly, lit in pts:
            px = to_px(lx, ly)
            if prev_px is not None:
                draw.line([prev_px, px], fill=COLOR_ON if lit else COLOR_OFF,
                          width=2 if lit else 1)
            if lit:
                draw.ellipse([px[0]-1, px[1]-1, px[0]+1, px[1]+1], fill=COLOR_ON)
            prev_px = px

        if pts:
            spx = to_px(pts[0][0], pts[0][1])
            draw.ellipse([spx[0]-5, spx[1]-5, spx[0]+5, spx[1]+5],
                         outline=(255, 200, 0), width=2)
            epx = to_px(pts[-1][0], pts[-1][1])
            draw.ellipse([epx[0]-5, epx[1]-5, epx[0]+5, epx[1]+5],
                         outline=(255, 80, 80), width=2)

        draw.rectangle([PAD, H-PAD+5, PAD+18, H-PAD+15], fill=COLOR_ON)
        draw.text((PAD+22, H-PAD+3), "Laser ON", fill=(200, 200, 200))
        draw.rectangle([PAD+120, H-PAD+5, PAD+138, H-PAD+15], fill=COLOR_OFF)
        draw.text((PAD+142, H-PAD+3), "Laser OFF (slew)", fill=(200, 200, 200))
        draw.ellipse([PAD+300, H-PAD+5, PAD+312, H-PAD+17],
                     outline=(255, 200, 0), width=2)
        draw.text((PAD+316, H-PAD+3), "Start", fill=(200, 200, 200))
        draw.ellipse([PAD+380, H-PAD+5, PAD+392, H-PAD+17],
                     outline=(255, 80, 80), width=2)
        draw.text((PAD+396, H-PAD+3), "End", fill=(200, 200, 200))

        img.save(out_path)

    def load_bf_logo(self, path: str) -> bool:
        """Parse an SVG and pre-compute Bigfoot logo scan points. Returns True on success."""
        try:
            pts = self._svg_to_scan_points(path)
        except Exception:
            return False
        if not pts:
            return False
        with self._lock:
            self._bf_logo_pts = pts
        return True

    def load_ae_logo(self, path: str) -> bool:
        """Parse an SVG and pre-compute AE Dynamics logo scan points. Returns True on success."""
        try:
            pts = self._svg_to_scan_points(path)
        except Exception:
            return False
        if not pts:
            return False
        with self._lock:
            self._ae_logo_pts = pts
        return True

    def set_bf_logo_mode(self, enabled: bool) -> None:
        with self._lock:
            self._bf_logo_mode = enabled

    def set_ae_logo_mode(self, enabled: bool) -> None:
        with self._lock:
            self._ae_logo_mode = enabled

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

    def set_neutral_beam(self, enabled: bool) -> None:
        with self._lock:
            self._neutral_beam = enabled

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
        fwd = _corrected_line_pts(lx0, ly0, lx1, ly1, n)
        rev = _corrected_line_pts(lx1, ly1, lx0, ly0, n)
        x_end, y_end = fwd[-1]  # corrected B position — same as rev[0]
        for i, (x, y) in enumerate(fwd):
            frame[i].x = x
            frame[i].y = y
            frame[i].g = 255
            frame[i].i = 255
        for i in range(_DWELL):
            frame[n + i].x = x_end  # park at corrected B so galvo is still during dwell
            frame[n + i].y = y_end
        for i, (x, y) in enumerate(rev):
            frame[n + _DWELL + i].x = x
            frame[n + _DWELL + i].y = y
            frame[n + _DWELL + i].g = 255
            frame[n + _DWELL + i].i = 255
        return frame, total

    @staticmethod
    def _build_rect_frame(lx0: int, ly0: int, lx1: int, ly1: int) -> tuple[ctypes.Array, int]:
        """Solid rectangle loop — all four sides fully lit, seamless frame wrap.

        Each side skips its start corner (covered by the previous corner's dwell) and
        ends with _RECT_CORNER_DWELL lit points so the galvo decelerates before turning.
        All four corners get identical dwell; the loop is fully symmetric.
        """
        density = _PERIM_PTS_PER_SIDE / 4095
        n_h = max(2, round(abs(lx1 - lx0) * density))
        n_v = max(2, round(abs(ly1 - ly0) * density))
        _CD = _RECT_CORNER_DWELL

        # Corrected corner x-positions: corners must lie on their respective V-edge columns.
        cx00 = _v_corrected_x(lx0, ly0)  # bottom-left
        cx10 = _v_corrected_x(lx1, ly0)  # bottom-right
        cx11 = _v_corrected_x(lx1, ly1)  # top-right
        cx01 = _v_corrected_x(lx0, ly1)  # top-left

        pts: list[tuple[int, int]] = []
        # Side 1: L→R bottom — skip [0] (start corner covered by prev frame's dwell)
        for x, y in _h_edge_pts(cx00, cx10, ly0, n_h)[1:]:
            pts.append((x, y))
        for _ in range(_CD):
            pts.append((cx10, ly0))
        # Side 2: B→T right
        for x, y in _v_edge_pts(lx1, ly0, ly1, n_v)[1:]:
            pts.append((x, y))
        for _ in range(_CD):
            pts.append((cx11, ly1))
        # Side 3: R→L top
        for x, y in _h_edge_pts(cx11, cx01, ly1, n_h)[1:]:
            pts.append((x, y))
        for _ in range(_CD):
            pts.append((cx01, ly1))
        # Side 4: T→B left — dwell lands on (cx00, ly0) for seamless frame wrap
        for x, y in _v_edge_pts(lx0, ly1, ly0, n_v)[1:]:
            pts.append((x, y))
        for _ in range(_CD):
            pts.append((cx00, ly0))

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
            _h_edge_pts(0, 4095, 0, n),        # bottom L→R
            _v_edge_pts(4095, 0, 4095, n),     # right  B→T
            _h_edge_pts(4095, 0, 4095, n),     # top    R→L
            _v_edge_pts(0, 4095, 0, n),        # left   T→B
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
        fwd_pts = _corrected_line_pts(lx0, ly0, lx1, ly1, m)
        x_end, y_end = fwd_pts[-1]  # corrected B — same as rev[0]
        for x, y in fwd_pts:
            pts.append((x, y, True))
        # Blank dwell at corrected B: galvo is stationary so laser turns back on cleanly.
        for _ in range(8):
            pts.append((x_end, y_end, False))
        for x, y in _corrected_line_pts(lx1, ly1, lx0, ly0, m):
            pts.append((x, y, True))
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
    def _build_perimeter_frame() -> tuple[ctypes.Array, int]:
        """Full-screen perimeter loop — solid, clockwise, seamless frame wrap at (0, 0)."""
        n = _PERIM_PTS_PER_SIDE
        sides = [
            _h_edge_pts(0, 4095, 0, n),        # bottom L→R
            _v_edge_pts(4095, 0, 4095, n),     # right  B→T
            _h_edge_pts(4095, 0, 4095, n),     # top    R→L
            _v_edge_pts(0, 4095, 0, n),        # left   T→B
        ]
        total = n * 4
        frame = (HeliosPoint * total)()
        idx = 0
        for side in sides:
            for x, y in side:
                frame[idx].x = x
                frame[idx].y = y
                frame[idx].g = 255
                frame[idx].i = 255
                idx += 1
        return frame, total

    @staticmethod
    def _build_rect_combined_frame(lx0: int, ly0: int, lx1: int, ly1: int) -> tuple[ctypes.Array, int]:
        """Full-screen perimeter + rectangle outline.

        Frame structure (transitions blanked):
          0. Blank head-dwell at (0, 0)
          1. Perimeter — clockwise, solid, ends at (0, 0)
          2. Blank transition (0, 0) → (lx0, ly0)
          3. Rectangle loop — all four sides lit, seamless, ends at (lx0, ly0)
          4. Blank tail (lx0, ly0) → (0, 0) — seamless into next frame's head dwell
        """
        pts: list[tuple[int, int, bool]] = []

        # 0. Blank head-dwell at (0, 0)
        for _ in range(20):
            pts.append((0, 0, False))

        # 1. Full-screen perimeter — clockwise from bottom-left
        n = _PERIM_PTS_PER_SIDE
        sides = [
            _h_edge_pts(0, 4095, 0, n),        # bottom L→R
            _v_edge_pts(4095, 0, 4095, n),     # right  B→T
            _h_edge_pts(4095, 0, 4095, n),     # top    R→L
            _v_edge_pts(0, 4095, 0, n),        # left   T→B
        ]
        for side in sides:
            for x, y in side:
                pts.append((x, y, True))

        # Corrected corner x-positions
        cx00 = _v_corrected_x(lx0, ly0)  # bottom-left
        cx10 = _v_corrected_x(lx1, ly0)  # bottom-right
        cx11 = _v_corrected_x(lx1, ly1)  # top-right
        cx01 = _v_corrected_x(lx0, ly1)  # top-left

        # 2. Blank transition: decelerate at (0,0) then slew to rectangle start
        for _ in range(20):
            pts.append((0, 0, False))
        for _ in range(60):
            pts.append((cx00, ly0, False))

        # 3. Rectangle loop with corner dwells — all lit
        density = _PERIM_PTS_PER_SIDE / 4095
        n_h = max(2, round(abs(lx1 - lx0) * density))
        n_v = max(2, round(abs(ly1 - ly0) * density))
        _CD = _RECT_CORNER_DWELL

        # Side 1: full range (galvo just settled at cx00,ly0 from blank transition)
        for x, y in _h_edge_pts(cx00, cx10, ly0, n_h):
            pts.append((x, y, True))
        for _ in range(_CD - 1):
            pts.append((cx10, ly0, True))
        # Sides 2-4: skip [0] (start corner already emitted by previous dwell)
        for x, y in _v_edge_pts(lx1, ly0, ly1, n_v)[1:]:
            pts.append((x, y, True))
        for _ in range(_CD - 1):
            pts.append((cx11, ly1, True))
        for x, y in _h_edge_pts(cx11, cx01, ly1, n_h)[1:]:
            pts.append((x, y, True))
        for _ in range(_CD - 1):
            pts.append((cx01, ly1, True))
        for x, y in _v_edge_pts(lx0, ly1, ly0, n_v)[1:]:
            pts.append((x, y, True))
        for _ in range(_CD - 1):
            pts.append((cx00, ly0, True))

        # Blank dwell: galvo decelerates at (cx00, ly0) before jumping to (0, 0)
        for _ in range(8):
            pts.append((cx00, ly0, False))

        # 4. Blank tail back to (0, 0)
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

        STEP = max(0.5, vb_w * 20 / 4095)
        BLANK_DWELL = 12
        CONTINUITY_THRESH = 10

        # --- Chain detection + nearest-neighbour reordering ---
        # First group paths into connected chains: a chain is a maximal sequence of paths
        # where each path's laser endpoint is within CONTINUITY_THRESH of the next path's
        # laser start.  Bigfoot's outline becomes one long chain and is never split;
        # the Death Star's 12 isolated lines each become a single-path chain.
        # NN reordering then operates on whole chains, preserving connectivity.

        def path_laser_endpoints(d_str, T):
            try:
                segs = [s for s in parse_path(d_str) if s.length() >= 0.01]
            except Exception:
                return None
            if not segs:
                return None
            p0, p1 = segs[0].point(0), segs[-1].point(1)
            sx, sy = apply_T(T, p0.real, p0.imag)
            ex, ey = apply_T(T, p1.real, p1.imag)
            return to_laser(sx, sy), to_laser(ex, ey)

        endpoints = [path_laser_endpoints(d, T) for d, T in path_list]

        # Build chains by extending each unvisited path forward.
        used = [False] * len(path_list)
        chains: list[list[int]] = []
        for start_idx in range(len(path_list)):
            if used[start_idx]:
                continue
            chain = [start_idx]
            used[start_idx] = True
            while True:
                ep = endpoints[chain[-1]]
                if ep is None:
                    break
                _, (ex, ey) = ep
                found = None
                for j in range(len(path_list)):
                    if used[j] or endpoints[j] is None:
                        continue
                    (sx, sy), _ = endpoints[j]
                    if abs(sx - ex) + abs(sy - ey) <= CONTINUITY_THRESH:
                        found = j
                        break
                if found is None:
                    break
                chain.append(found)
                used[found] = True
            chains.append(chain)

        # Laser endpoints for each chain: start of first path, end of last path.
        def chain_laser_endpoints(chain):
            ep0 = endpoints[chain[0]]
            ep1 = endpoints[chain[-1]]
            if ep0 is None and ep1 is None:
                return None
            if ep0 is None:
                return ep1[0], ep1[1]
            if ep1 is None:
                return ep0[0], ep0[1]
            return ep0[0], ep1[1]

        chain_eps = [chain_laser_endpoints(c) for c in chains]

        # NN reorder chains; each chain may be traversed reversed end-to-end.
        remaining = list(range(len(chains)))
        ordered_chains: list[tuple[list[int], bool]] = []
        cur_lx, cur_ly = 2048, 2048

        while remaining:
            best_ci, best_cost, best_rev = remaining[0], float('inf'), False
            for ci in remaining:
                ep = chain_eps[ci]
                if ep is None:
                    cost, rev = 0, False
                else:
                    (sx, sy), (ex, ey) = ep
                    d_fwd = abs(sx - cur_lx) + abs(sy - cur_ly)
                    d_rev = abs(ex - cur_lx) + abs(ey - cur_ly)
                    cost, rev = (d_fwd, False) if d_fwd <= d_rev else (d_rev, True)
                if cost < best_cost:
                    best_cost, best_ci, best_rev = cost, ci, rev
            remaining.remove(best_ci)
            ordered_chains.append((chains[best_ci], best_rev))
            ep = chain_eps[best_ci]
            if ep is not None:
                (sx, sy), (ex, ey) = ep
                cur_lx, cur_ly = (sx, sy) if best_rev else (ex, ey)

        # Flatten to (d_str, T, is_reversed) in render order.
        ordered: list[tuple[str, tuple, bool]] = []
        for chain, chain_rev in ordered_chains:
            for idx in (reversed(chain) if chain_rev else chain):
                ordered.append((path_list[idx][0], path_list[idx][1], chain_rev))

        # --- Render in reordered sequence, with per-path reversal support ---
        result: list[tuple[int, int, bool]] = []
        last_lx: int | None = None
        last_ly: int | None = None
        last_rendered = False  # True only when last_lx/last_ly reflects actual lit output

        for d_str, T, is_reversed in ordered:
            try:
                path = parse_path(d_str)
            except Exception:
                continue
            segs = list(path)
            if is_reversed:
                segs = list(reversed(segs))
            if not segs:
                continue

            t_scale = math.hypot(T[0], T[1])
            prev_end_raw = None

            for seg in segs:
                # In reversed mode the segment runs from point(1) → point(0)
                seg_start = seg.point(1 if is_reversed else 0)
                sx, sy = apply_T(T, seg_start.real, seg_start.imag)
                lx0, ly0 = to_laser(sx, sy)

                seg_len = seg.length()
                if seg_len < 0.01:
                    prev_end_raw = seg.point(0 if is_reversed else 1)
                    ex, ey = apply_T(T, prev_end_raw.real, prev_end_raw.imag)
                    last_lx, last_ly = to_laser(ex, ey)
                    last_rendered = False  # degenerate segment has no lit output here
                    continue

                jump_dist = (abs(lx0 - last_lx) + abs(ly0 - last_ly)) if last_lx is not None else 0
                is_gap = (
                    last_lx is None
                    or (prev_end_raw is not None and abs(seg_start - prev_end_raw) > 1e-6)
                    or jump_dist > CONTINUITY_THRESH
                )
                if is_gap:
                    if last_lx is not None and last_rendered:
                        # Blank dwell at the segment end — keeps the laser OFF while the
                        # galvo decelerates, avoiding overshoot smear at the endpoint.
                        for _ in range(6):
                            result.append((last_lx, last_ly, False))
                    # Scale settle time with jump distance so the galvo is fully stopped
                    # before the next segment starts.  A fixed 12-point dwell (0.4 ms) is
                    # fine for short hops but not for jumps > ~600 DAC units.
                    settle = max(BLANK_DWELL, min(50, jump_dist // 20))
                    for _ in range(settle):
                        result.append((lx0, ly0, False))

                n = max(2, int(seg_len * t_scale / STEP) + 1)
                for k in range(n):
                    t = (1 - k / (n - 1)) if is_reversed else (k / (n - 1))
                    p = seg.point(t)
                    px, py = apply_T(T, p.real, p.imag)
                    lx, ly = to_laser(px, py)
                    result.append((lx, ly, True))

                prev_end_raw = seg.point(0 if is_reversed else 1)
                ex, ey = apply_T(T, prev_end_raw.real, prev_end_raw.imag)
                last_lx, last_ly = to_laser(ex, ey)
                last_rendered = True

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
                neutral_beam = self._neutral_beam
                line_pts = self._line_pts
                first_pt = self._first_pt
                show_perimeter = self._show_perimeter
                bf_logo_mode = self._bf_logo_mode
                bf_logo_pts = self._bf_logo_pts
                ae_logo_mode = self._ae_logo_mode
                ae_logo_pts = self._ae_logo_pts
                rect_coords = self._rect_coords

            if neutral_beam:
                frame, n = self._build_point_frame(2048, 2048)
                pps = _LINE_PPS
            elif ae_logo_mode and ae_logo_pts is not None:
                frame, n = self._build_logo_frame(ae_logo_pts)
                pps = _LOGO_PPS
            elif bf_logo_mode and bf_logo_pts is not None:
                frame, n = self._build_logo_frame(bf_logo_pts)
                pps = _LOGO_PPS
            elif rect_coords is not None:
                if show_perimeter:
                    frame, n = self._build_rect_combined_frame(*rect_coords)
                else:
                    frame, n = self._build_rect_frame(*rect_coords)
                pps = _LINE_PPS
            elif line_pts is not None:
                if show_perimeter:
                    frame, n = self._build_combined_frame(*line_pts)
                else:
                    frame, n = self._build_line_frame(*line_pts)
                pps = _LINE_PPS
            elif show_perimeter:
                frame, n = self._build_perimeter_frame()
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
