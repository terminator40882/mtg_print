"""Kalibrierung: Ausgleich von Druckversatz und Maßstabsfehlern des Druckers.

Modell (pro Achse): gedruckte_Position = scale * pdf_Position + offset
(gemessen in mm von der linken bzw. oberen Papierkante).

Damit ein Element an der Soll-Position t landet, wird im PDF an
(t - offset) / scale gezeichnet. Das geschieht über eine einzige
Transformationsmatrix pro Seite, d.h. *alles* (Bilder, Schnittmarken) wird
gleich korrigiert.
"""
from __future__ import annotations

import uuid

# Positionen der Messlinien auf der Kalibrierseite (mm von linker/oberer Kante)
REF = {"A": 20.0, "B": 190.0, "C": 20.0, "D": 277.0}

DEFAULT_PROFILE = {
    "id": "default",
    "name": "Standard (unkalibriert)",
    "offset_x": 0.0,
    "offset_y": 0.0,
    "scale_x": 1.0,
    "scale_y": 1.0,
    "notes": "",
    "measurements": None,
}


def new_profile(name: str, base: dict | None = None) -> dict:
    p = dict(base or DEFAULT_PROFILE)
    p["id"] = uuid.uuid4().hex[:8]
    p["name"] = name or "Neues Profil"
    return p


def from_measurements(a: float, b: float, c: float, d: float, ref: dict = REF) -> dict:
    """Berechnet offset/scale aus den vier gemessenen Abständen.

    a: linke Kante -> Linie A   (Soll 20 mm)
    b: linke Kante -> Linie B   (Soll 190 mm)
    c: obere Kante -> Linie C   (Soll 20 mm)
    d: obere Kante -> Linie D   (Soll 277 mm)
    """
    sx = (b - a) / (ref["B"] - ref["A"])
    sy = (d - c) / (ref["D"] - ref["C"])
    if not (0.9 < sx < 1.1 and 0.9 < sy < 1.1):
        raise ValueError(
            "Die Messwerte ergeben einen Maßstab von "
            f"{sx * 100:.1f}% / {sy * 100:.1f}% – das ist unplausibel. "
            "Bitte prüfen, ob von der Papierkante zur Mitte der Linie gemessen wurde "
            "und die Seite mit 100% (ohne 'An Seite anpassen') gedruckt wurde.")
    ox = a - sx * ref["A"]
    oy = c - sy * ref["C"]
    return {"offset_x": round(ox, 3), "offset_y": round(oy, 3),
            "scale_x": round(sx, 6), "scale_y": round(sy, 6)}


def combine(current: dict, measured: dict) -> dict:
    """Wenn die Kalibrierseite bereits MIT einem Profil gedruckt wurde, ergibt die
    neue Messung den Restfehler. Neue Gesamtkorrektur = Restfehler ∘ alt.

    gedruckt = s_m * pdf' + o_m, wobei pdf' = (t - o_alt)/s_alt das bereits
    korrigierte Ziel war -> gesamt: s = s_m*s_alt, o = o_alt*s_m + o_m ... wir
    rechnen es sauber über die Abbildung t -> gedruckt aus.
    """
    out = {}
    for ax in ("x", "y"):
        s_old, o_old = current[f"scale_{ax}"], current[f"offset_{ax}"]
        s_m, o_m = measured[f"scale_{ax}"], measured[f"offset_{ax}"]
        # Gemessen wurde: printed = s_m * t + o_m, obwohl im PDF (t - o_old)/s_old stand.
        # Drucker-Modell: printed = S * pdf + O  mit pdf = (t - o_old)/s_old
        # => S/s_old = s_m  und  O - S*o_old/s_old = o_m
        S = s_m * s_old
        O = o_m + S * o_old / s_old
        out[f"scale_{ax}"] = round(S, 6)
        out[f"offset_{ax}"] = round(O, 3)
    return out


def pdf_matrix(profile: dict | None, page_h_mm: float) -> tuple[float, ...] | None:
    """Matrix (a, b, c, d, e, f) in PDF-Punkten für ``canvas.transform``.

    Gezeichnet wird in normalen PDF-Koordinaten (Ursprung unten links).
    """
    if not profile:
        return None
    sx, sy = float(profile.get("scale_x", 1.0)), float(profile.get("scale_y", 1.0))
    ox, oy = float(profile.get("offset_x", 0.0)), float(profile.get("offset_y", 0.0))
    if sx == 1.0 and sy == 1.0 and ox == 0.0 and oy == 0.0:
        return None
    pt = 72.0 / 25.4
    h = page_h_mm * pt
    # x_out = x/sx - ox/sx ;  y_out = y/sy + H - (H - oy)/sy   (y nach oben, oy nach unten)
    return (1 / sx, 0.0, 0.0, 1 / sy, -ox * pt / sx, h - (h - oy * pt) / sy)


def describe(profile: dict) -> list[str]:
    """Menschlich lesbare Beschreibung der Korrektur."""
    lines = []
    ox, oy = profile.get("offset_x", 0), profile.get("offset_y", 0)
    sx, sy = profile.get("scale_x", 1), profile.get("scale_y", 1)
    if abs(ox) >= 0.01:
        lines.append(f"Drucker setzt {abs(ox):.2f} mm zu weit {'rechts' if ox > 0 else 'links'} → wird ausgeglichen")
    if abs(oy) >= 0.01:
        lines.append(f"Drucker setzt {abs(oy):.2f} mm zu weit {'unten' if oy > 0 else 'oben'} → wird ausgeglichen")
    if abs(sx - 1) >= 1e-5:
        lines.append(f"Horizontaler Maßstab {sx * 100:.3f}% → wird ausgeglichen")
    if abs(sy - 1) >= 1e-5:
        lines.append(f"Vertikaler Maßstab {sy * 100:.3f}% → wird ausgeglichen")
    return lines or ["Keine Korrektur aktiv"]
