"""Bildverarbeitung: Bleed-Erkennung, Normalisierung, synthetischer Bleed, Thumbnails.

Jedes Kartenbild wird intern als "Karte + Bleed" behandelt. ``prepare`` liefert
ein RGB-Bild und den Bleed-Anteil pro Seite (als Bruchteil der Bildbreite /
-höhe). Bilder ohne Padding bekommen einen synthetischen Bleed (Randpixel werden
nach außen verlängert), damit auch Außenkanten sauber geschnitten werden können.
"""
from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageOps

from . import config

Image.MAX_IMAGE_PIXELS = 400_000_000  # große MPC-Scans (1200 DPI) zulassen

BLEED_MODES = {
    "mpc": "MPC-Fill (erkennt 0,12\" / 1/8\" pro Bild)",
    "eighth": "Fest 1/8 Zoll (3,175 mm)",
    "auto": "Automatisch (auch „kein Padding“)",
    "custom": "Eigene Breite (mm)",
    "none": "Kein Padding",
}

_IN = config.MM_PER_INCH
# Bekannte Bleed-Konventionen: (Name, Kartenbreite in, Kartenhöhe in, Bleed in)
CONVENTIONS = [
    ("MPC-Vorlage 2,5×3,5\" + 0,12\"", 2.5, 3.5, 0.12),
    ("2,5×3,5\" + 1/8\"", 2.5, 3.5, 0.125),
    ("63×88 mm + 3,048 mm (MPC-Fill)", 63 / _IN, 88 / _IN, 3.048 / _IN),
    ("63×88 mm + 1/8\"", 63 / _IN, 88 / _IN, 0.125),
]
NO_BLEED_CONVENTIONS = [
    ("ohne Padding 63×88 mm", 63 / _IN, 88 / _IN, 0.0),
    ("ohne Padding 2,5×3,5\"", 2.5, 3.5, 0.0),
]
MAX_MISMATCH = 0.005  # 0,5 % DPI-Unterschied zwischen den Achsen


def match_convention(w: int, h: int, include_none: bool = False) -> dict:
    """Welche Konvention erklärt die Bildgröße am besten?

    Bei der richtigen Annahme ist die Auflösung (DPI) auf beiden Achsen gleich.
    Echte Beispiele: 2192×2992 / 3288×4488 = MPC-Vorlage bei exakt 800 / 1200 DPI,
    2176×2960 = 63×88 mm + 1/8\" bei 797 DPI.
    """
    if w > h:
        w, h = h, w
    best = None
    for name, cw, ch, b in CONVENTIONS + (NO_BLEED_CONVENTIONS if include_none else []):
        dx, dy = w / (cw + 2 * b), h / (ch + 2 * b)
        mismatch = abs(dx / dy - 1)
        if best is None or mismatch < best["mismatch"]:
            best = {"name": name, "mismatch": mismatch, "dpi": (dx + dy) / 2,
                    "fx": b / (cw + 2 * b), "fy": b / (ch + 2 * b), "bleed_mm": b * _IN}
    return best

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


def detect_bleed_mm(w: int, h: int, card_w_mm: float = config.DEFAULT_CARD_W_MM,
                    card_h_mm: float = config.DEFAULT_CARD_H_MM) -> float:
    """Gleichmäßiger Bleed (mm pro Seite) aus dem Seitenverhältnis, Karte = 63 x 88 mm
    (wie MPC-Fill). Aus w = k*63 + 2b, h = k*88 + 2b folgt k = (h-w)/25 px/mm."""
    if h <= w:
        return 0.0
    k = (h - w) / (card_h_mm - card_w_mm)
    b = (w - card_w_mm * k) / 2 / k
    return max(0.0, min(b, 8.0))


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
    if mode in ("mpc", "auto"):
        m = match_convention(w, h, include_none=(mode == "auto"))
        if m["mismatch"] > MAX_MISMATCH and mode == "mpc":
            return _fractions_from_inches(config.MPC_BLEED_IN)  # unbekannt -> MPC-Vorlage
        if m["mismatch"] > MAX_MISMATCH:
            b = detect_bleed_mm(w, h, card_w_mm, card_h_mm)
            return (0.0, 0.0) if b < 0.8 else (b / (card_w_mm + 2 * b), b / (card_h_mm + 2 * b))
        return m["fx"], m["fy"]
    if mode == "eighth":
        return _fractions_from_inches(config.EIGHTH_INCH_BLEED_IN)
    if mode == "custom":
        b = max(0.0, float(custom_mm or 0.0))
        return b / (card_w_mm + 2 * b), b / (card_h_mm + 2 * b)
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


# ---------------------------------------------------------------------------
# PDF-Einbettung: Durchreichen, Aufbereiten, Cache
# ---------------------------------------------------------------------------
def _is_srgb(icc: bytes | None) -> bool:
    if not icc:
        return True
    try:
        desc = ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc)))
        return "srgb" in desc.lower()
    except Exception:
        return False


def _passthrough(path: Path, card: dict, max_dpi: float | None, card_w_mm: float, card_h_mm: float):
    """Original-JPEG unverändert einbetten, wenn keine Bearbeitung nötig ist
    (schnell und ohne erneute JPEG-Kompression)."""
    if int(card.get("rotation", 0)) % 360 or card.get("bleed_mode") == "none":
        return None
    with Image.open(path) as im:
        if im.format != "JPEG" or im.mode not in ("RGB", "L"):
            return None
        if im.getexif().get(0x0112, 1) not in (0, 1):
            return None
        if not _is_srgb(im.info.get("icc_profile")):
            return None
        w, h = im.size
    fx, fy = bleed_fractions(card.get("bleed_mode", "mpc"), w, h, card.get("bleed_mm"), card_w_mm, card_h_mm)
    if fx <= 0 or fy <= 0:
        return None
    if max_dpi:
        dpi = w * (1 - 2 * fx) / (card_w_mm / config.MM_PER_INCH)
        if dpi > max_dpi * 1.02:
            return None
    return path, fx, fy


def pdf_image(path: Path, card: dict, quality: str, max_dpi: float | None,
              synthetic_bleed_mm: float, card_w_mm: float, card_h_mm: float) -> tuple[Path | bytes, float, float]:
    """Liefert (Datei oder Bilddaten, fx, fy) für die PDF – mit Festplatten-Cache.

    Bevorzugt einen Dateipfad: reportlab erkennt Duplikate dann am Pfad statt jedes
    Bild bei jedem Einsatz komplett zu dekodieren und zu hashen."""
    pt = _passthrough(path, card, max_dpi, card_w_mm, card_h_mm)
    if pt:
        return pt
    st = path.stat()
    key_src = json.dumps([path.name, st.st_size, int(st.st_mtime), quality, max_dpi, synthetic_bleed_mm,
                          card_w_mm, card_h_mm, card.get("bleed_mode"), card.get("bleed_mm"),
                          card.get("rotation"), card.get("fill_corners", True), card.get("bleed_fill"), 3])
    key = hashlib.sha1(key_src.encode()).hexdigest()[:20]
    data_path = config.CACHE_DIR / f"{key}.bin"
    meta_path = config.CACHE_DIR / f"{key}.json"
    try:
        meta = json.loads(meta_path.read_text())
        if data_path.exists():
            return data_path, meta["fx"], meta["fy"]
    except (OSError, ValueError, KeyError):
        pass
    p = prepare(path, card, max_dpi=max_dpi, synthetic_bleed_mm=synthetic_bleed_mm,
                card_w_mm=card_w_mm, card_h_mm=card_h_mm)
    data = encode_for_pdf(p, quality).getvalue()
    try:
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = data_path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(data_path)
        meta_path.write_text(json.dumps({"fx": p.fx, "fy": p.fy}))
        return data_path, p.fx, p.fy
    except OSError:
        return data, p.fx, p.fy


def prune_cache(max_bytes: int = 3 * 1024 ** 3) -> None:
    files = sorted(config.CACHE_DIR.glob("*.bin"), key=lambda f: f.stat().st_atime, reverse=True)
    total = 0
    for f in files:
        total += f.stat().st_size
        if total > max_bytes:
            f.unlink(missing_ok=True)
            f.with_suffix(".json").unlink(missing_ok=True)
