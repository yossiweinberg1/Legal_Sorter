"""Shared icon generation utility.

Generates a 32x32 LegalSorter .ico file using only stdlib (struct + zlib),
so no Pillow or other third-party dependency is required.  The ICO uses a
PNG payload (Vista+ compatible) for crisp rendering on high-DPI displays.

Design: dark-navy background, gold border, blue left pillar,
        amber balance-scale beam and pans.
"""
import struct
import zlib
from pathlib import Path


def make_icon_ico(dest: Path) -> None:
    """Write (or overwrite) *dest* with a fresh LegalSorter .ico file."""
    W = H = 32
    # Colours as (R, G, B)
    NAVY  = (0x1f, 0x24, 0x38)
    GOLD  = (0xf5, 0xc5, 0x42)
    BLUE  = (0x2e, 0x86, 0xde)
    AMBER = (0xf3, 0x9c, 0x12)

    def _px(x: int, y: int) -> tuple:
        # Gold border (2 px)
        if x < 2 or x >= W - 2 or y < 2 or y >= H - 2:
            return GOLD
        # Blue left pillar (cols 4–8, rows 4–27)
        if 4 <= x <= 8 and 4 <= y <= 27:
            return BLUE
        # Amber horizontal beam (row 14, cols 9–26)
        if y == 14 and 9 <= x <= 26:
            return AMBER
        # Amber vertical centre post (col 17, rows 6–13)
        if x == 17 and 6 <= y <= 13:
            return AMBER
        # Left pan (3-px wide, rows 18–20, cols 10–14)
        if 10 <= x <= 14 and 18 <= y <= 20:
            return AMBER
        # Right pan (3-px wide, rows 20–22, cols 20–24)
        if 20 <= x <= 24 and 20 <= y <= 22:
            return AMBER
        # Base post (col 17, rows 24–27)
        if x == 17 and 24 <= y <= 27:
            return AMBER
        # Base feet (row 27, cols 13–21)
        if y == 27 and 13 <= x <= 21:
            return AMBER
        return NAVY

    # Build raw PNG scanlines (filter byte 0 = None per row)
    scanlines = b"".join(
        b"\x00" + bytes(c for x in range(W) for c in _px(x, y))
        for y in range(H)
    )

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(scanlines, 9))
        + _chunk(b"IEND", b"")
    )

    # ICO wrapper: 6-byte file header + 16-byte ICONDIRENTRY + PNG payload
    ico_header = struct.pack("<HHH", 0, 1, 1)
    ico_entry  = struct.pack("<BBBBHHII", W, H, 0, 0, 1, 32, len(png), 22)
    dest.write_bytes(ico_header + ico_entry + png)


def ensure_icon(dest: Path) -> str:
    """Generate the .ico at *dest* if it does not already exist.

    Returns the absolute path string, suitable for use as ``IconLocation``
    in a Windows .lnk shortcut or ``wm_iconbitmap`` in Tkinter.
    """
    if not dest.exists():
        make_icon_ico(dest)
    return str(dest)
