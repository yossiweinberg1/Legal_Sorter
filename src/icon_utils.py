"""Shared icon generation utility.

Generates a 32x32 LegalSorter .ico file using only stdlib (struct + zlib),
so no Pillow or other third-party dependency is required.  The ICO uses a
PNG payload (Vista+ compatible) for crisp rendering on high-DPI displays.

Design: dark-navy background (#1E2D40), teal balance-scale beam, pans, and
        pillar (#3E9BC0), steel-gray document stack (#8FA3B1).
"""
import struct
import zlib
from pathlib import Path


def make_icon_ico(dest: Path) -> None:
    """Write (or overwrite) *dest* with a fresh LegalSorter .ico file."""
    W = H = 32
    # Colours as (R, G, B) — new Legal Sorter branding palette
    NAVY  = (0x1E, 0x2D, 0x40)  # dark-navy background
    TEAL  = (0x3E, 0x9B, 0xC0)  # teal scale / highlights
    GRAY  = (0x8F, 0xA3, 0xB1)  # steel-gray document

    def _px(x: int, y: int) -> tuple:
        # ── Scale beam (row 12, cols 6–25) ──────────────────────────────
        if y == 12 and 6 <= x <= 25:
            return TEAL
        # ── Centre post (col 15, rows 4–11 and 20–27) ───────────────────
        if x == 15 and (4 <= y <= 11 or 20 <= y <= 27):
            return TEAL
        # ── Left pan (cols 4–9, rows 15–17) ─────────────────────────────
        if 4 <= x <= 9 and 15 <= y <= 17:
            return TEAL
        # ── Right pan (cols 20–25, rows 15–17) ──────────────────────────
        if 20 <= x <= 25 and 15 <= y <= 17:
            return TEAL
        # ── Left chain (col 7, rows 13–14) ──────────────────────────────
        if x == 7 and 13 <= y <= 14:
            return TEAL
        # ── Right chain (col 22, rows 13–14) ────────────────────────────
        if x == 22 and 13 <= y <= 14:
            return TEAL
        # ── Document stack (cols 10–20, rows 20–25) ─────────────────────
        if 10 <= x <= 20 and 20 <= y <= 22:
            return GRAY
        if 11 <= x <= 19 and 23 <= y <= 25:
            return GRAY
        # ── Base feet (row 27, cols 11–19) ──────────────────────────────
        if y == 27 and 11 <= x <= 19:
            return TEAL
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
