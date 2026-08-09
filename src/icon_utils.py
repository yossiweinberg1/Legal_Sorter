"""Shared Legal Sorter branding utilities."""

from __future__ import annotations

import base64
import math
import struct
import zlib
from functools import lru_cache
from pathlib import Path

NAVY = (22, 38, 58)
TEAL = (32, 184, 211)
CYAN = (72, 210, 224)
SLATE = (88, 101, 121)
WHITE = (255, 255, 255)


def _point_segment_distance(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx = bx - ax
    dy = by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    nx = ax + t * dx
    ny = ay + t * dy
    return math.hypot(px - nx, py - ny)


def _fill_circle(canvas: list[list[tuple[int, int, int]]], cx: float, cy: float, radius: float, color: tuple[int, int, int]) -> None:
    min_x = max(0, int(cx - radius - 1))
    max_x = min(len(canvas[0]) - 1, int(cx + radius + 1))
    min_y = max(0, int(cy - radius - 1))
    max_y = min(len(canvas) - 1, int(cy + radius + 1))
    radius_sq = radius * radius
    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            dx = x + 0.5 - cx
            dy = y + 0.5 - cy
            if dx * dx + dy * dy <= radius_sq:
                canvas[y][x] = color


def _draw_line(
    canvas: list[list[tuple[int, int, int]]],
    start: tuple[float, float],
    end: tuple[float, float],
    color: tuple[int, int, int],
    width: float,
) -> None:
    ax, ay = start
    bx, by = end
    radius = width / 2.0
    min_x = max(0, int(min(ax, bx) - radius - 1))
    max_x = min(len(canvas[0]) - 1, int(max(ax, bx) + radius + 1))
    min_y = max(0, int(min(ay, by) - radius - 1))
    max_y = min(len(canvas) - 1, int(max(ay, by) + radius + 1))
    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            if _point_segment_distance(x + 0.5, y + 0.5, ax, ay, bx, by) <= radius:
                canvas[y][x] = color


def _draw_polyline(
    canvas: list[list[tuple[int, int, int]]],
    points: list[tuple[float, float]],
    color: tuple[int, int, int],
    width: float,
) -> None:
    for start, end in zip(points, points[1:]):
        _draw_line(canvas, start, end, color, width)


def _point_in_polygon(px: float, py: float, points: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(points) - 1
    for i, (xi, yi) in enumerate(points):
        xj, yj = points[j]
        intersects = ((yi > py) != (yj > py)) and (
            px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-9) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def _fill_polygon(
    canvas: list[list[tuple[int, int, int]]],
    points: list[tuple[float, float]],
    color: tuple[int, int, int],
) -> None:
    min_x = max(0, int(min(x for x, _ in points)))
    max_x = min(len(canvas[0]) - 1, int(max(x for x, _ in points) + 1))
    min_y = max(0, int(min(y for _, y in points)))
    max_y = min(len(canvas) - 1, int(max(y for _, y in points) + 1))
    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            if _point_in_polygon(x + 0.5, y + 0.5, points):
                canvas[y][x] = color


def _fill_lower_ellipse(
    canvas: list[list[tuple[int, int, int]]],
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    color: tuple[int, int, int],
) -> None:
    min_x = max(0, int(cx - rx - 1))
    max_x = min(len(canvas[0]) - 1, int(cx + rx + 1))
    min_y = max(0, int(cy))
    max_y = min(len(canvas) - 1, int(cy + ry + 1))
    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            dx = (x + 0.5 - cx) / rx
            dy = (y + 0.5 - cy) / ry
            if dx * dx + dy * dy <= 1.0:
                canvas[y][x] = color


# Transparent sentinel – pixels that should remain fully transparent.
_TRANSPARENT: tuple[int, int, int] = (-1, -1, -1)


@lru_cache(maxsize=None)
def _logo_png_bytes(size: int) -> bytes:
    # Use transparent background so the icon blends with any desktop colour.
    canvas: list[list[tuple[int, int, int]]] = [[_TRANSPARENT for _ in range(size)] for _ in range(size)]
    s = size / 100.0
    pt = lambda x, y: (x * s, y * s)

    # ------------------------------------------------------------------ #
    # Open book at bottom (navy spine + teal page edges fanning outward)  #
    # ------------------------------------------------------------------ #
    # Book spine / centre crease
    _draw_line(canvas, pt(50, 72), pt(50, 90), NAVY, 2.4 * s)
    # Left teal page arcs (approximated as polylines fanning up-left)
    _draw_polyline(canvas, [pt(50, 72), pt(35, 68), pt(15, 72), pt(8, 79)], TEAL, 3.6 * s)
    _draw_polyline(canvas, [pt(50, 76), pt(36, 73), pt(18, 77), pt(11, 83)], TEAL, 2.6 * s)
    # Left navy page (slightly inside the teal)
    _draw_polyline(canvas, [pt(50, 79), pt(37, 76), pt(20, 80), pt(14, 86)], NAVY, 2.2 * s)
    # Right teal page arcs
    _draw_polyline(canvas, [pt(50, 72), pt(65, 68), pt(85, 72), pt(92, 79)], TEAL, 3.6 * s)
    _draw_polyline(canvas, [pt(50, 76), pt(64, 73), pt(82, 77), pt(89, 83)], TEAL, 2.6 * s)
    # Right navy page
    _draw_polyline(canvas, [pt(50, 79), pt(63, 76), pt(80, 80), pt(86, 86)], NAVY, 2.2 * s)
    # Bottom of book (flat base)
    _draw_line(canvas, pt(14, 86), pt(86, 86), NAVY, 2.8 * s)

    # ------------------------------------------------------------------ #
    # Hexagonal shield body (dark navy + slate interior highlight)        #
    # ------------------------------------------------------------------ #
    # Hex shield outline (flat-top hexagon, pointing left/right)
    # Six vertices centred at (50, 55), width ≈ 40, height ≈ 36
    hex_pts = [
        pt(50, 35),   # top
        pt(68, 43),   # upper-right
        pt(68, 63),   # lower-right
        pt(50, 72),   # bottom
        pt(32, 63),   # lower-left
        pt(32, 43),   # upper-left
    ]
    _fill_polygon(canvas, hex_pts, NAVY)
    # Inner highlight (slightly smaller hex for 3-D look)
    inner_scale = 0.80
    cx, cy = 50, 53.5
    inner_pts = [
        pt(cx + (px - cx) * inner_scale, cy + (py - cy) * inner_scale)
        for (px, py) in [(50, 35), (68, 43), (68, 63), (50, 72), (32, 63), (32, 43)]
    ]
    _fill_polygon(canvas, inner_pts, SLATE)

    # ------------------------------------------------------------------ #
    # S-shaped routing / sorting icon inside the shield (teal accents)   #
    # ------------------------------------------------------------------ #
    # Upper-left arrow block (upper arm of the S)
    _fill_polygon(canvas, [pt(36, 42), pt(52, 42), pt(52, 47), pt(41, 47), pt(41, 52), pt(36, 52)], SLATE)
    # Upper-right teal accent bar
    _fill_polygon(canvas, [pt(54, 42), pt(64, 42), pt(64, 46), pt(54, 46)], TEAL)
    # Arrow head pointing right on upper arm
    _fill_polygon(canvas, [pt(48, 39), pt(55, 43), pt(48, 47)], NAVY)
    # Lower-right arrow block (lower arm of the S)
    _fill_polygon(canvas, [pt(48, 55), pt(64, 55), pt(64, 60), pt(59, 60), pt(59, 65), pt(48, 65)], SLATE)
    # Lower-left teal accent bar
    _fill_polygon(canvas, [pt(36, 61), pt(46, 61), pt(46, 65), pt(36, 65)], TEAL)
    # Arrow head pointing left on lower arm
    _fill_polygon(canvas, [pt(52, 52), pt(45, 57), pt(52, 61)], NAVY)
    # Small teal routing node (centre dot) and connector line
    _fill_circle(canvas, 50 * s, 53.5 * s, 2.6 * s, TEAL)
    _fill_circle(canvas, 38 * s, 57 * s, 2.0 * s, CYAN)
    _draw_line(canvas, pt(38, 57), pt(50, 53.5), CYAN, 1.6 * s)

    # ------------------------------------------------------------------ #
    # Scales of justice at top                                            #
    # ------------------------------------------------------------------ #
    # Center pillar (vertical bar from top of hex to tip finial)
    _draw_line(canvas, pt(50, 10), pt(50, 35), NAVY, 4.4 * s)
    # Diamond / flame finial at very top
    _fill_polygon(canvas, [pt(50, 4), pt(53, 8), pt(50, 13), pt(47, 8)], NAVY)

    # Main beam (slightly arched polyline)
    _draw_polyline(canvas, [pt(15, 20), pt(28, 17), pt(50, 14), pt(72, 17), pt(85, 20)], NAVY, 3.8 * s)
    # Centre pivot knob
    _fill_circle(canvas, 50 * s, 14 * s, 2.6 * s, NAVY)
    # End knobs on beam
    _fill_circle(canvas, 15 * s, 20 * s, 2.0 * s, NAVY)
    _fill_circle(canvas, 85 * s, 20 * s, 2.0 * s, NAVY)

    # Left hanging chains
    _draw_line(canvas, pt(21, 22), pt(14, 36), NAVY, 1.8 * s)
    _draw_line(canvas, pt(21, 22), pt(28, 36), NAVY, 1.8 * s)
    # Right hanging chains
    _draw_line(canvas, pt(79, 22), pt(72, 36), NAVY, 1.8 * s)
    _draw_line(canvas, pt(79, 22), pt(86, 36), NAVY, 1.8 * s)

    # Left pan (flat top rim + lower half-ellipse)
    _draw_line(canvas, pt(10, 36), pt(32, 36), NAVY, 2.4 * s)
    _fill_lower_ellipse(canvas, 21 * s, 36 * s, 11 * s, 7 * s, NAVY)
    # Right pan
    _draw_line(canvas, pt(68, 36), pt(90, 36), NAVY, 2.4 * s)
    _fill_lower_ellipse(canvas, 79 * s, 36 * s, 11 * s, 7 * s, NAVY)

    # Small teal triangle accents inside each pan (decorative highlights)
    _fill_polygon(canvas, [pt(17, 37), pt(21, 37), pt(19, 40)], TEAL)
    _fill_polygon(canvas, [pt(75, 37), pt(79, 37), pt(77, 40)], TEAL)

    # Build RGBA scanlines (color type 6: R G B A per pixel).
    def _pixel_rgba(p: tuple[int, int, int]) -> bytes:
        if p is _TRANSPARENT:
            return b"\x00\x00\x00\x00"
        return bytes(p) + b"\xff"

    scanlines = b"".join(
        b"\x00" + b"".join(_pixel_rgba(pixel) for pixel in row)
        for row in canvas
    )

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    return (
        b"\x89PNG\r\n\x1a\n"
        # color_type=6 → RGBA (required for PNG-in-ICO on Windows)
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(scanlines, 9))
        + _chunk(b"IEND", b"")
    )


def make_logo_png(dest: Path, size: int = 256) -> None:
    """Write the shared Legal Sorter logo PNG."""
    dest.write_bytes(_logo_png_bytes(size))


def ensure_logo_png(dest: Path, size: int = 256, force: bool = True) -> str:
    """Generate the shared Legal Sorter logo PNG.

    Pass ``force=False`` to skip regeneration when the file already exists.
    The default regenerates on every call so stale cached icons are always
    replaced with the current design.
    """
    if force or not dest.exists():
        make_logo_png(dest, size=size)
    return str(dest)


def logo_data_uri(size: int = 256) -> str:
    """Return a base64 data URI for the shared Legal Sorter logo PNG."""
    return "data:image/png;base64," + base64.b64encode(_logo_png_bytes(size)).decode("ascii")


def make_icon_ico(dest: Path) -> None:
    """Write the Windows .ico variant of the shared Legal Sorter logo.

    Uses a 256×256 RGBA PNG embedded in the ICO container.  A size value
    of 256 is stored as 0 in the ICO directory entry per the ICO spec.
    """
    size = 256
    png = _logo_png_bytes(size)
    # Per the ICO spec, a width/height of 256 is encoded as 0 in the entry.
    ico_dim = 0
    ico_header = struct.pack("<HHH", 0, 1, 1)
    ico_entry = struct.pack("<BBBBHHII", ico_dim, ico_dim, 0, 0, 1, 32, len(png), 22)
    dest.write_bytes(ico_header + ico_entry + png)


def ensure_icon(dest: Path, force: bool = True) -> str:
    """Generate the shared Legal Sorter .ico and return its path.

    Pass ``force=False`` to skip regeneration when the file already exists.
    The default regenerates on every call so stale cached icons are always
    replaced with the current design.
    """
    if force or not dest.exists():
        make_icon_ico(dest)
    return str(dest)
