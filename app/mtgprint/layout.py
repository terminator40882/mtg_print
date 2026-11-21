"""Seitengeometrie: Position der Karten-Slots, Bleed-Bereiche und Schnittlinien.

Alle Maße in Millimetern, Ursprung oben links auf dem Blatt (wie beim Messen
mit dem Lineal). Die Umrechnung in PDF-Koordinaten passiert erst in ``pdfgen``.
"""
from __future__ import annotations

import math
from copy import deepcopy

from . import config

DEFAULT_LAYOUT = {
    "paper": "A4",
    "card_w": config.DEFAULT_CARD_W_MM,
    "card_h": config.DEFAULT_CARD_H_MM,
    "cols": 0,             # 0 = automatisch (A4: 3)
    "rows": 0,             # 0 = automatisch (A4: 3)
    "gap": 0.0,            # Abstand zwischen Karten; 0 = Karten stoßen aneinander (8 Schnitte)
    "rotate_cards": False, # Karten um 90° gedreht (quer) platzieren, z.B. 13x18 mit Rand
    "borderless": False,   # Randlos drucken (z.B. 13x18: 2x2 Karten)
    "outer_bleed": 3.0,    # wie weit das Bild an den Außenkanten über die Schnittlinie reicht
    "offset_x": 0.0,       # bewusste Verschiebung des Rasters (zusätzlich zur Kalibrierung)
    "offset_y": 0.0,
    "min_margin": 4.0,     # Mindestrand für die automatische Spalten/Zeilen-Berechnung
    "cut_marks": True,
    "cut_mark_len": 0.0,   # 0 = bis zum Blattrand
    "cut_mark_width": 0.2,
    "cut_mark_gap": 1.0,   # Abstand Schnittmarke <-> Bildkante
    "cut_mark_color": "#000000",
    "page_label": True,
    "quality": "high",     # original | high | standard
    "synthetic_bleed": config.SYNTHETIC_BLEED_MM,
}

LIMITS = {
    "card_w": (20.0, 200.0), "card_h": (20.0, 280.0), "cols": (0, 10), "rows": (0, 10),
    "gap": (0.0, 20.0), "outer_bleed": (0.0, 10.0), "offset_x": (-30.0, 30.0),
    "offset_y": (-30.0, 30.0), "min_margin": (0.0, 30.0), "cut_mark_len": (0.0, 50.0),
    "cut_mark_width": (0.05, 2.0), "cut_mark_gap": (0.0, 10.0), "synthetic_bleed": (0.0, 6.0),
}


def normalize_layout(data: dict | None) -> dict:
    out = deepcopy(DEFAULT_LAYOUT)
    for key, value in (data or {}).items():
        if key not in DEFAULT_LAYOUT:
            continue
        default = DEFAULT_LAYOUT[key]
        try:
            if isinstance(default, bool):
                value = bool(value)
            elif isinstance(default, int):
                value = int(value)
            elif isinstance(default, float):
                value = float(value)
            else:
                value = str(value)
        except (TypeError, ValueError):
            continue
        if key in LIMITS:
            lo, hi = LIMITS[key]
            value = type(default)(min(max(value, lo), hi))
        out[key] = value
    if out["paper"] not in config.PAPER_SIZES_MM:
        out["paper"] = "A4"
    if out["quality"] not in ("original", "high", "standard"):
        out["quality"] = "high"
    return out


def slot_size(layout: dict) -> tuple[float, float]:
    """Größe eines Slots auf dem Blatt (bei gedrehten Karten vertauscht)."""
    if layout.get("rotate_cards"):
        return layout["card_h"], layout["card_w"]
    return layout["card_w"], layout["card_h"]


def grid_size(layout: dict) -> tuple[int, int]:
    pw, ph = config.PAPER_SIZES_MM[layout["paper"]]
    cw, ch = slot_size(layout)
    gap = layout["gap"]
    m = 0.0 if layout.get("borderless") else layout["min_margin"]
    auto_c = max(1, math.floor((pw - 2 * m + gap) / (cw + gap) + 1e-9))
    auto_r = max(1, math.floor((ph - 2 * m + gap) / (ch + gap) + 1e-9))
    return (layout["cols"] or auto_c, layout["rows"] or auto_r)


def slots_per_page(layout: dict) -> int:
    c, r = grid_size(layout)
    return c * r


def geometry(layout: dict) -> dict:
    """Berechnet alle Rechtecke einer Seite (unabhängig von den Bildern)."""
    layout = normalize_layout(layout)
    pw, ph = config.PAPER_SIZES_MM[layout["paper"]]
    cols, rows = grid_size(layout)
    cw, ch = slot_size(layout)
    gap = layout["gap"]
    grid_w = cols * cw + (cols - 1) * gap
    grid_h = rows * ch + (rows - 1) * gap
    x0 = (pw - grid_w) / 2 + layout["offset_x"]
    y0 = (ph - grid_h) / 2 + layout["offset_y"]
    outer, inner = layout["outer_bleed"], gap / 2

    slots = []
    for r in range(rows):
        for c in range(cols):
            x = x0 + c * (cw + gap)
            y = y0 + r * (ch + gap)
            slots.append({
                "index": r * cols + c, "row": r, "col": c, "rotated": bool(layout["rotate_cards"]),
                "x": x, "y": y, "w": cw, "h": ch,
                # gewünschte Bleed-Ausdehnung je Seite (wird durch das Bild begrenzt)
                "bleed": {
                    "l": outer if c == 0 else inner,
                    "r": outer if c == cols - 1 else inner,
                    "t": outer if r == 0 else inner,
                    "b": outer if r == rows - 1 else inner,
                },
            })

    def uniq(values):
        out = []
        for v in sorted(values):
            if not out or abs(v - out[-1]) > 1e-6:
                out.append(round(v, 4))
        return out

    cuts_x = uniq([s["x"] for s in slots] + [s["x"] + cw for s in slots])
    cuts_y = uniq([s["y"] for s in slots] + [s["y"] + ch for s in slots])
    pm = config.PRINTER_MARGINS_MM
    margins = {"l": x0, "r": pw - x0 - grid_w, "t": y0, "b": ph - y0 - grid_h}
    if layout["borderless"]:
        fits = min(margins.values()) >= -1e-6
        printable = fits
    else:
        fits = x0 - outer >= 0 and y0 - outer >= 0 and x0 + grid_w + outer <= pw and y0 + grid_h + outer <= ph
        printable = all(margins[k] >= pm[k] - 1e-6 for k in pm)
    return {
        "page_w": pw, "page_h": ph, "cols": cols, "rows": rows,
        "paper": layout["paper"], "borderless": layout["borderless"],
        "rotate_cards": layout["rotate_cards"],
        "printable": printable,
        "grid": {"x": x0, "y": y0, "w": grid_w, "h": grid_h},
        "outer_bleed": outer,
        "slots": slots, "cuts_x": cuts_x, "cuts_y": cuts_y,
        "num_cuts": len(cuts_x) + len(cuts_y),
        "margins": margins,
        "fits": fits,
    }


def reflow(pages: list[list], per_page: int) -> list[list]:
    """Slots auf eine neue Seitengröße umbrechen (Reihenfolge bleibt erhalten)."""
    flat = [s for p in pages for s in p]
    while flat and flat[-1] is None:
        flat.pop()
    out = [flat[i:i + per_page] for i in range(0, len(flat), per_page)] or [[]]
    for p in out:
        p.extend([None] * (per_page - len(p)))
    return out


# Fertige Voreinstellungen (in der Oberfläche als Buttons)
PRESETS = {
    "a4": {"label": "A4 · 9 Karten", "layout": {"paper": "A4", "borderless": False, "rotate_cards": False,
                                                 "cols": 0, "rows": 0, "gap": 0.0, "offset_x": 0.0, "offset_y": 0.0}},
    "13x18-borderless": {"label": "13×18 randlos · 4 Karten",
                         "layout": {"paper": "13x18", "borderless": True, "rotate_cards": False,
                                    "cols": 2, "rows": 2, "gap": 0.0, "offset_x": 0.0, "offset_y": 0.0}},
    "13x18": {"label": "13×18 mit Rand · 2 Karten quer",
              "layout": {"paper": "13x18", "borderless": False, "rotate_cards": True,
                         "cols": 1, "rows": 2, "gap": 0.0, "offset_x": 0.0, "offset_y": 0.0}},
}


def paper_key(layout: dict) -> str:
    """Schlüssel für Papier + Randlos (Kalibrierprofile gelten je Papiersorte)."""
    return layout["paper"] + ("-randlos" if layout.get("borderless") else "")
