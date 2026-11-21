"""Import von MPC-Fill-Bestellungen (XML).

Eine MPC-Fill-XML enthält pro Kartenbild die Drive-ID, den Namen und die
Slot-Nummern (= Reihenfolge und Anzahl). Wir ordnen die XML-Einträge den
hochgeladenen Bildern zu (über die Drive-ID im Dateinamen oder den Namen) und
können fehlende Bilder optional direkt von Google Drive laden.
"""
from __future__ import annotations

import re
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from .storage import clean_name


def parse_order(xml_bytes: bytes) -> dict:
    root = ET.fromstring(xml_bytes)

    def cards(section: str) -> list[dict]:
        out = []
        sec = root.find(section)
        if sec is None:
            return out
        for c in sec.findall("card"):
            slots_text = (c.findtext("slots") or "").strip()
            slots = [int(s) for s in re.split(r"[,\s]+", slots_text) if s.strip().isdigit()]
            out.append({
                "id": (c.findtext("id") or "").strip(),
                "name": (c.findtext("name") or "").strip(),
                "query": (c.findtext("query") or "").strip(),
                "slots": slots,
            })
        return out

    details = root.find("details")
    quantity = int(details.findtext("quantity") or 0) if details is not None else 0
    return {
        "quantity": quantity,
        "fronts": cards("fronts"),
        "backs": cards("backs"),
        "cardback": (root.findtext("cardback") or "").strip(),
    }


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", clean_name(s).lower())


def match_cards(entries: list[dict], cards: dict) -> dict[str, str | None]:
    """Ordnet XML-Einträge (Drive-ID) Bibliothekskarten zu."""
    by_drive = {}
    by_name = {}
    for cid, c in cards.items():
        orig = c.get("original_name", "")
        if c.get("drive_id"):
            by_drive[c["drive_id"]] = cid
        for m in re.finditer(r"[A-Za-z0-9_\-]{20,}", orig):
            by_drive.setdefault(m.group(0), cid)
        by_name.setdefault(_norm(orig), cid)
        by_name.setdefault(_norm(c.get("name", "")), cid)
    out = {}
    for e in entries:
        cid = by_drive.get(e["id"])
        if not cid and e["name"]:
            cid = by_name.get(_norm(e["name"]))
        out[e["id"]] = cid
    return out


def download_drive_image(drive_id: str, dest_dir: Path, timeout: float = 60) -> Path:
    """Lädt ein Bild von Google Drive (öffentliche MPC-Fill-Bilder)."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]{10,}", drive_id):
        raise ValueError("ungültige Drive-ID")
    url = f"https://drive.google.com/uc?export=download&id={drive_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (MTG Print Studio)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = resp.headers.get("Content-Type", "")
        data = resp.read()
        disp = resp.headers.get("Content-Disposition", "")
    if not ctype.startswith("image/"):
        raise RuntimeError("Google Drive lieferte kein Bild (Limit erreicht oder Datei privat?)")
    m = re.search(r'filename="?([^";]+)"?', disp)
    ext = Path(m.group(1)).suffix if m else ("." + ctype.split("/")[1].split(";")[0])
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{drive_id}{ext.lower()}"
    path.write_bytes(data)
    return path
