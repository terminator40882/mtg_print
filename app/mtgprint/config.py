"""Zentrale Konfiguration (Umgebungsvariablen, Pfade, physikalische Konstanten)."""
from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else default


DATA_DIR = Path(_env("DATA_DIR", "/data"))
CARDS_DIR = DATA_DIR / "cards"          # Originalbilder
THUMBS_DIR = DATA_DIR / "thumbs"        # Vorschaubilder (inkl. Bleed)
PROJECTS_DIR = DATA_DIR / "projects"    # Projekt-JSONs
EXPORTS_DIR = DATA_DIR / "exports"      # erzeugte PDFs
CACHE_DIR = DATA_DIR / "cache"          # aufbereitete Bilder für PDFs (beschleunigt Wiederholungen)
SETTINGS_FILE = DATA_DIR / "settings.json"
CARDS_FILE = DATA_DIR / "cards.json"

# --- Drucker -------------------------------------------------------------
PRINTER_IP = _env("PRINTER_IP")
PRINTER_URI = _env("PRINTER_URI")  # optional, überschreibt PRINTER_IP
PRINTER_QUEUE = _env("PRINTER_QUEUE", "Canon_G650") or "Canon_G650"
# "auto" = automatisch Glossy II / Glossy aus den vom Drucker gemeldeten Typen wählen
PRINT_MEDIA_TYPE = _env("PRINT_MEDIA_TYPE", "auto") or "auto"
PRINT_QUALITY = _env("PRINT_QUALITY", "high") or "high"   # draft | normal | high
PRINT_INPUT_SLOT = _env("PRINT_INPUT_SLOT", "auto") or "auto"


def printer_uri() -> str:
    """IPP-URI des Druckers (treiberlos / IPP Everywhere / AirPrint)."""
    if PRINTER_URI:
        return PRINTER_URI
    if PRINTER_IP:
        return f"ipp://{PRINTER_IP}:631/ipp/print"
    return ""


# --- Physik --------------------------------------------------------------
MM_PER_INCH = 25.4

# Originale Magic-Karten: 63 x 88 mm
DEFAULT_CARD_W_MM = 63.0
DEFAULT_CARD_H_MM = 88.0

# MPC / MPC-Fill-Bilder: Karte 2.5" x 3.5" plus Bleed. Laut MakePlayingCards
# ist die Vorlage 822 x 1122 px @ 300 DPI = 2.74" x 3.74", also 0.12" (3.05 mm)
# pro Seite. Umgangssprachlich wird das oft "1/8 Zoll" (0.125" = 3.175 mm)
# genannt; beides ist als Preset wählbar.
MPC_CARD_W_IN = 2.5
MPC_CARD_H_IN = 3.5
MPC_BLEED_IN = 0.12
EIGHTH_INCH_BLEED_IN = 0.125

# Bleed, der für Bilder OHNE Padding synthetisch erzeugt wird (mm)
SYNTHETIC_BLEED_MM = 3.0

PAPER_SIZES_MM = {
    "A4": (210.0, 297.0),
    "13x18": (127.0, 177.8),   # 13 x 18 cm Fotopapier = 5 x 7 Zoll
    "Letter": (215.9, 279.4),
}
PAPER_LABELS = {
    "A4": "A4 (210 × 297 mm)",
    "13x18": "13 × 18 cm Foto (5 × 7″)",
    "Letter": "US Letter",
}
# Nicht bedruckbarer Rand bei Druck MIT Rand (Canon PIXMA: oben 3, unten 5, seitlich 3,4 mm)
PRINTER_MARGINS_MM = {"l": 3.4, "r": 3.4, "t": 3.0, "b": 5.0}

# Parallelität (Bildaufbereitung, Thumbnails). 0 = alle Kerne
WORKERS = int(_env("WORKERS", "0") or 0) or (os.cpu_count() or 2)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}


def ensure_dirs() -> None:
    for d in (DATA_DIR, CARDS_DIR, THUMBS_DIR, PROJECTS_DIR, EXPORTS_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)
