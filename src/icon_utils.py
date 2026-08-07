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


@lru_cache(maxsize=None)
def _logo_png_bytes(size: int) -> bytes:
    canvas = [[WHITE for _ in range(size)] for _ in range(size)]
    s = size / 100.0
    pt = lambda x, y: (x * s, y * s)

    # Bottom book pages.
    _draw_polyline(canvas, [pt(10, 78), pt(24, 72), pt(38, 75), pt(50, 82)], TEAL, 3.2 * s)
    _draw_polyline(canvas, [pt(13, 83), pt(27, 78), pt(40, 80), pt(50, 86)], NAVY, 2.8 * s)
    _draw_polyline(canvas, [pt(50, 82), pt(62, 75), pt(76, 72), pt(90, 78)], TEAL, 3.2 * s)
    _draw_polyline(canvas, [pt(50, 86), pt(60, 80), pt(73, 78), pt(87, 83)], NAVY, 2.8 * s)

    # Center stacked documents / chevrons.
    _fill_polygon(canvas, [pt(30, 42), pt(58, 54), pt(46, 64), pt(18, 52)], NAVY)
    _fill_polygon(canvas, [pt(26, 56), pt(54, 68), pt(42, 78), pt(14, 66)], NAVY)
    _fill_polygon(canvas, [pt(43, 61), pt(71, 73), pt(59, 83), pt(31, 71)], NAVY)
    _fill_polygon(canvas, [pt(38, 44), pt(57, 52), pt(52, 57), pt(33, 49)], SLATE)
    _draw_line(canvas, pt(52, 45), pt(73, 54), TEAL, 4.4 * s)
    _fill_circle(canvas, 53 * s, 55 * s, 2.4 * s, TEAL)
    _fill_circle(canvas, 28 * s, 63 * s, 2.2 * s, CYAN)
    _fill_circle(canvas, 36 * s, 67 * s, 1.8 * s, SLATE)
    _draw_line(canvas, pt(28, 63), pt(36, 67), CYAN, 1.6 * s)

    # Scale beam and center pillar.
    _draw_polyline(canvas, [pt(18, 19), pt(30, 16), pt(40, 20), pt(50, 15), pt(60, 20), pt(70, 16), pt(82, 19)], NAVY, 4.1 * s)
    _draw_line(canvas, pt(50, 15), pt(50, 41), NAVY, 4.4 * s)
    _fill_polygon(canvas, [pt(50, 6), pt(53, 10), pt(50, 14), pt(47, 10)], NAVY)
    _fill_circle(canvas, 18 * s, 19 * s, 2.0 * s, NAVY)
    _fill_circle(canvas, 82 * s, 19 * s, 2.0 * s, NAVY)
    _fill_circle(canvas, 50 * s, 15 * s, 2.8 * s, NAVY)

    # Hangers.
    _draw_line(canvas, pt(24, 22), pt(15, 43), NAVY, 2.0 * s)
    _draw_line(canvas, pt(24, 22), pt(33, 43), NAVY, 2.0 * s)
    _draw_line(canvas, pt(76, 22), pt(67, 43), NAVY, 2.0 * s)
    _draw_line(canvas, pt(76, 22), pt(85, 43), NAVY, 2.0 * s)

    # Pans.
    _fill_lower_ellipse(canvas, 24 * s, 45 * s, 12 * s, 8 * s, NAVY)
    _fill_lower_ellipse(canvas, 76 * s, 45 * s, 12 * s, 8 * s, NAVY)
    _draw_line(canvas, pt(12, 45), pt(36, 45), NAVY, 2.4 * s)
    _draw_line(canvas, pt(64, 45), pt(88, 45), NAVY, 2.4 * s)

    scanlines = b"".join(
        b"\x00" + b"".join(bytes(pixel) for pixel in row)
        for row in canvas
    )

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(scanlines, 9))
        + _chunk(b"IEND", b"")
    )


def make_logo_png(dest: Path, size: int = 256) -> None:
    """Write the shared Legal Sorter logo PNG."""
    dest.write_bytes(_logo_png_bytes(size))


def ensure_logo_png(dest: Path, size: int = 256) -> str:
    """Generate the shared Legal Sorter logo PNG if needed."""
    if not dest.exists():
        make_logo_png(dest, size=size)
    return str(dest)


def logo_data_uri(size: int = 256) -> str:
    """Return a base64 data URI for the shared Legal Sorter logo PNG."""
    return "data:image/png;base64," + base64.b64encode(_logo_png_bytes(size)).decode("ascii")


def make_icon_ico(dest: Path) -> None:
    """Write the Windows .ico variant of the shared Legal Sorter logo."""
    size = 128
    png = _logo_png_bytes(size)
    ico_dim = size if size < 256 else 0
    ico_header = struct.pack("<HHH", 0, 1, 1)
    ico_entry = struct.pack("<BBBBHHII", ico_dim, ico_dim, 0, 0, 1, 32, len(png), 22)
    dest.write_bytes(ico_header + ico_entry + png)


def ensure_icon(dest: Path) -> str:
    """Generate the shared Legal Sorter .ico if needed and return its path."""
    if not dest.exists():
        make_icon_ico(dest)
    return str(dest)
