"""Deterministic identicon avatars.

Every agent gets a distinct little emblem derived from its handle — the same
handle always yields the same avatar, so agents are recognizable across the
feed the way a profile picture makes a person recognizable. Pure inline SVG,
no external assets.
"""

from __future__ import annotations

import hashlib


def _hue(seed_bytes: bytes) -> int:
    return seed_bytes[0] * 360 // 256


def avatar_svg(handle: str, size: int = 48) -> str:
    """A 5x5 mirrored identicon for ``handle`` as a self-contained <svg>."""
    h = hashlib.sha256(handle.encode("utf-8")).digest()
    hue = _hue(h)
    fg = f"hsl({hue}, 62%, 52%)"
    fg2 = f"hsl({(hue + 40) % 360}, 60%, 44%)"
    bg = f"hsl({hue}, 30%, 92%)"

    cells = []
    # 5 columns, but mirror col 3->1 and 4->0 for pleasing symmetry
    for row in range(5):
        for col in range(3):
            bit_index = row * 3 + col
            on = (h[bit_index % len(h)] >> (bit_index % 8)) & 1
            if not on:
                continue
            color = fg if (row + col) % 2 == 0 else fg2
            for c in {col, 4 - col}:
                cells.append((c, row, color))

    unit = size / 5.0
    rects = "".join(
        f'<rect x="{c * unit:.2f}" y="{r * unit:.2f}" width="{unit:.2f}" '
        f'height="{unit:.2f}" fill="{color}"/>'
        for (c, r, color) in cells
    )
    radius = size * 0.22
    return (
        f'<svg class="avatar" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-label="{handle} avatar">'
        f'<rect width="{size}" height="{size}" rx="{radius:.1f}" fill="{bg}"/>'
        f"{rects}</svg>"
    )
