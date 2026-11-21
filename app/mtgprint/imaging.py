"""Bildverarbeitung: Bleed-Erkennung, Normalisierung, synthetischer Bleed, Thumbnails.

Jedes Kartenbild wird intern als "Karte + Bleed" behandelt. ``prepare`` liefert
ein RGB-Bild und den Bleed-Anteil pro Seite (als Bruchteil der Bildbreite /
-höhe). Bilder ohne Padding bekommen einen synthetischen Bleed (Randpixel werden
nach außen verlängert), damit auch Außenkanten sauber geschnitten werden können.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageOps

from . import config

Image.MAX_IMAGE_PIXELS = 400_000_000  # große MPC-Scans (1200 DPI) zulassen

BLEED_MODES = {
    "mpc": "MPC-Fill / MPC (0,12\" ≈ 3,05 mm)",
    "eighth": "1/8 Zoll (3,175 mm)",
    "auto": "Automatisch (aus Seitenverhältnis)",
    "custom": "Eigene Breite (mm)",
    "none": "Kein Padding",
}

NO_BLEED_RATIO = config.DEFAULT_CARD_W_MM / config.DEFAULT_CARD_H_MM            # 0.716
MPC_RATIO = (config.MPC_CARD_W_IN + 2 * config.MPC_BLEED_IN) / (
    config.MPC_CARD_H_IN + 2 * config.MPC_BLEED_IN)                              # 0.733


@dataclass
class Prepared:
    image: Image.Image   # RGB
    fx: float            # Bleed links/rechts als Anteil der Bildbreite
    fy: float            # Bleed oben/unten als Anteil der Bildhöhe
    synthetic: bool      # Bleed wurde künstlich erzeugt

    @property
    def card_px(self) -> tuple[float, float]:
        w, h = self.image.size
        return w * (1 - 2 * self.fx), h * (1 - 2 * self.fy)


# ---------------------------------------------------------------------------
# Bleed-Berechnung
# ---------------------------------------------------------------------------
def _fractions_from_inches(b_in: float) -> tuple[float, float]:
    return (b_in / (config.MPC_CARD_W_IN + 2 * b_in),
            b_in / (config.MPC_CARD_H_IN + 2 * b_in))


def detect_bleed_inches(w: int, h: int) -> float:
    """Schätzt den (gleichmäßigen) Bleed in Zoll aus dem Seitenverhältnis,
    unter der Annahme, dass die Karte selbst 2.5" x 3.5" groß ist."""
    if h <= w:
        return 0.0
    b = (3.5 * w - 2.5 * h) / (2 * (h - w))
    return max(0.0, min(b, 0.3))


def suggest_mode(w: int, h: int) -> str:
    """Welche Einstellung passt zum Seitenverhältnis? ('mpc' oder 'none')."""
    if w > h:
        w, h = h, w
    r = w / h
    return "none" if abs(r - NO_BLEED_RATIO) < abs(r - MPC_RATIO) else "mpc"


def bleed_fractions(mode: str, w: int, h: int, custom_mm: float | None = None,
                    card_w_mm: float = config.DEFAULT_CARD_W_MM,
                    card_h_mm: float = config.DEFAULT_CARD_H_MM) -> tuple[float, float]:
    """Bleed-Anteil (fx, fy) pro Seite für ein Bild der Größe w x h (bereits rotiert)."""
    if mode == "mpc":
        return _fractions_from_inches(config.MPC_BLEED_IN)
    if mode == "eighth":
        return _fractions_from_inches(config.EIGHTH_INCH_BLEED_IN)
    if mode == "custom":
        b = max(0.0, float(custom_mm or 0.0))
        return b / (card_w_mm + 2 * b), b / (card_h_mm + 2 * b)
    if mode == "auto":
        b_in = detect_bleed_inches(w, h)
        if b_in < 0.03:
            return 0.0, 0.0
        return _fractions_from_inches(b_in)
    return 0.0, 0.0


# ---------------------------------------------------------------------------
# Bild laden / normalisieren
# ---------------------------------------------------------------------------
def open_image(path: Path) -> Image.Image:
    im = Image.open(path)
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:  # pragma: no cover - defekte EXIF-Daten
        pass
    return im


def image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        return im.size


def _to_srgb(im: Image.Image) -> Image.Image:
    icc = im.info.get("icc_profile")
    if icc and im.mode in ("RGB", "RGBA", "CMYK"):
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            dst = ImageCms.createProfile("sRGB")
            out_mode = "RGBA" if im.mode == "RGBA" else "RGB"
            return ImageCms.profileToProfile(im, src, dst, outputMode=out_mode)
        except Exception:
            pass
    if im.mode == "CMYK":
        return im.convert("RGB")
    return im


def _border_color(rgba: Image.Image) -> tuple[int, int, int]:
    """Medianfarbe des Kartenrands (für das Füllen transparenter Ecken)."""
    small = rgba.copy()
    small.thumbnail((240, 336))
    a = np.asarray(small.convert("RGBA"))
    h, w = a.shape[:2]
    i0, i1 = max(1, int(w * 0.012)), max(2, int(w * 0.045))
    mask = np.zeros((h, w), bool)
    mask[:, i0:i1] = True
    mask[:, w - i1:w - i0] = True
    mask[i0:i1, :] = True
    mask[h - i1:h - i0, :] = True
    mask &= a[..., 3] > 250
    if mask.sum() < 10:
        return (0, 0, 0)
    px = a[..., :3][mask]
    return tuple(int(v) for v in np.median(px, axis=0))


def _flatten(im: Image.Image, fill_corners: bool) -> Image.Image:
    if im.mode == "P":
        im = im.convert("RGBA")
    if im.mode in ("RGBA", "LA"):
        rgba = im.convert("RGBA")
        alpha = rgba.getchannel("A")
        if alpha.getextrema()[0] < 255:
            color = _border_color(rgba) if fill_corners else (255, 255, 255)
            bg = Image.new("RGB", rgba.size, color)
            bg.paste(rgba, mask=alpha)
            return bg
        return rgba.convert("RGB")
    return im.convert("RGB")


def _pad(im: Image.Image, px_x: int, px_y: int, mode: str) -> Image.Image:
    if px_x <= 0 and px_y <= 0:
        return im
    a = np.asarray(im)
    np_mode = "symmetric" if mode == "mirror" else "edge"
    a = np.pad(a, ((px_y, px_y), (px_x, px_x), (0, 0)), mode=np_mode)
    return Image.fromarray(a)


def prepare(path: Path, card: dict, max_dpi: float | None = None,
            synthetic_bleed_mm: float = config.SYNTHETIC_BLEED_MM,
            card_w_mm: float = config.DEFAULT_CARD_W_MM,
            card_h_mm: float = config.DEFAULT_CARD_H_MM) -> Prepared:
    """Lädt ein Kartenbild und liefert es normalisiert als Karte + Bleed."""
    im = open_image(path)
    rotation = int(card.get("rotation", 0)) % 360
    if rotation == 90:
        im = im.transpose(Image.Transpose.ROTATE_270)
    elif rotation == 180:
        im = im.transpose(Image.Transpose.ROTATE_180)
    elif rotation == 270:
        im = im.transpose(Image.Transpose.ROTATE_90)

    w, h = im.size
    mode = card.get("bleed_mode", "mpc")
    fx, fy = bleed_fractions(mode, w, h, card.get("bleed_mm"), card_w_mm, card_h_mm)

    # Auflösung begrenzen (spart Speicher & Druckzeit, ohne sichtbaren Verlust)
    if max_dpi:
        card_px_w = w * (1 - 2 * fx)
        dpi = card_px_w / (card_w_mm / config.MM_PER_INCH)
        if dpi > max_dpi * 1.02:
            scale = max_dpi / dpi
            new = (max(1, round(w * scale)), max(1, round(h * scale)))
            if im.mode not in ("RGB", "RGBA", "L", "LA", "CMYK"):
                im = im.convert("RGBA")
            im = im.resize(new, Image.Resampling.LANCZOS, reducing_gap=3.0)
            w, h = im.size

    im = _to_srgb(im)
    im = _flatten(im, bool(card.get("fill_corners", True)))

    synthetic = False
    if fx <= 0 and fy <= 0 and synthetic_bleed_mm > 0:
        px_per_mm_x = w / card_w_mm
        px_per_mm_y = h / card_h_mm
        pad_x = max(1, round(synthetic_bleed_mm * px_per_mm_x))
        pad_y = max(1, round(synthetic_bleed_mm * px_per_mm_y))
        im = _pad(im, pad_x, pad_y, card.get("bleed_fill", "edge"))
        fx = pad_x / im.size[0]
        fy = pad_y / im.size[1]
        synthetic = True
    return Prepared(im, fx, fy, synthetic)


def effective_dpi(w: int, h: int, card: dict) -> float:
    rotation = int(card.get("rotation", 0)) % 180
    if rotation == 90:
        w, h = h, w
    fx, _ = bleed_fractions(card.get("bleed_mode", "mpc"), w, h, card.get("bleed_mm"))
    return w * (1 - 2 * fx) / (config.DEFAULT_CARD_W_MM / config.MM_PER_INCH)


def make_thumbnail(path: Path, card: dict, out: Path, width_px: int = 360) -> dict:
    """Thumbnail (Karte + Bleed) erzeugen. Gibt die Bleed-Anteile zurück."""
    dpi = width_px / (config.DEFAULT_CARD_W_MM / config.MM_PER_INCH)
    p = prepare(path, card, max_dpi=dpi)
    out.parent.mkdir(parents=True, exist_ok=True)
    p.image.save(out, "JPEG", quality=85, optimize=True)
    return {"fx": p.fx, "fy": p.fy, "synthetic": p.synthetic}


def encode_for_pdf(p: Prepared, quality: str) -> io.BytesIO:
    """Bild für die PDF-Einbettung kodieren (JPEG wird von reportlab 1:1 übernommen)."""
    buf = io.BytesIO()
    if quality == "original":
        p.image.save(buf, "PNG", optimize=False, compress_level=6)
    else:
        q = 95 if quality == "high" else 90
        p.image.save(buf, "JPEG", quality=q, subsampling=0 if quality == "high" else 2, optimize=True)
    buf.seek(0)
    return buf


QUALITY_MAX_DPI = {"original": None, "high": 1200, "standard": 600}
