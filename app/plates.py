"""Plate geometry helpers: presets and microplate well-name parsing."""
from __future__ import annotations

import re

# Standard microplate footprint presets: (rows, columns)
PLATE_PRESETS: dict[str, tuple[int, int]] = {
    "24": (4, 6),
    "48": (6, 8),
    "96": (8, 12),
    "384": (16, 24),
}

# Limits for custom plate types. Keep the CP model comfortably within bounds.
MAX_DIM = 50
MAX_CELLS = 400

# Canonical well name: Excel-style column letters followed by the row number,
# e.g. "A1" ... "H12" for a 96-well plate.
_WELL_RE = re.compile(r"^([A-Z]+)([0-9]+)$")


def well_name(row: int, col: int) -> str:
    """Return the canonical name of zero-indexed position (row, col)."""
    return f"{chr(ord('A') + row)}{col + 1}"


def parse_well(text: str, rows: int, cols: int) -> tuple[int, int]:
    """Parse a well name into zero-indexed (row, col).

    Raises ValueError when the name is malformed or outside the plate.
    """
    m = _WELL_RE.match(text.strip().upper())
    if not m:
        raise ValueError(f"invalid well name '{text}', expected e.g. 'A1'")
    letters, digits = m.groups()
    r = ord(letters[0]) - ord("A")
    if len(letters) != 1 or not (0 <= r < rows):
        raise ValueError(f"well '{text}' row '{letters}' outside the plate (A-{chr(ord('A') + rows - 1)})")
    c = int(digits) - 1
    if not (0 <= c < cols):
        raise ValueError(f"well '{text}' column {digits} outside the plate (1-{cols})")
    return r, c
