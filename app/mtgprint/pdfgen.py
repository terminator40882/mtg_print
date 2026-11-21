"""PDF-Erzeugung: Kartenbögen, Kalibrierseite und Probeschnitt-Seite."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from reportlab.lib.colors import Color, HexColor
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas as rl_canvas

from . import calibration, config, imaging
from .layout import geometry, normalize_layout

PT = 72.0 / 25.4  # PDF-Punkte pro mm


class Page:
    """Hilfsklasse: zeichnet in mm mit Ursprung oben links."""

    def __init__(self, c: rl_canvas.Canvas, page_h: float):
        self.c = c
        self.H = page_h

    def X(self, x):
        return x * PT

    def Y(self, y):
        return (self.H - y) * PT

    def line(self, x1, y1, x2, y2, width=0.2, color="#000000"):
        c = self.c
        c.setStrokeColor(HexColor(color))
        c.setLineWidth(width * PT)
        c.line(self.X(x1), self.Y(y1), self.X(x2), self.Y(y2))

    def rect(self, x, y, w, h, stroke=None, fill=None, width=0.2):
        c = self.c
        if stroke:
            c.setStrokeColor(HexColor(stroke) if isinstance(stroke, str) else stroke)
            c.setLineWidth(width * PT)
        if fill:
            c.setFillColor(HexColor(fill) if isinstance(fill, str) else fill)
        c.rect(self.X(x), self.Y(y + h), w * PT, h * PT, stroke=1 if stroke else 0, fill=1 if fill else 0)

    def text(self, x, y, s, size=8, color="#000000", font="Helvetica", align="left", max_w=None):
        c = self.c
        if max_w:
            while size > 4 and stringWidth(s, font, size) > max_w * PT:
                size -= 0.25
            while len(s) > 4 and stringWidth(s, font, size) > max_w * PT:
                s = s.rstrip("…")[:-1] + "…"
        c.setFillColor(HexColor(color))
        c.setFont(font, size)
        if align == "center":
            c.drawCentredString(self.X(x), self.Y(y), s)
        elif align == "right":
            c.drawRightString(self.X(x), self.Y(y), s)
        else:
            c.drawString(self.X(x), self.Y(y), s)

    def clip_image(self, reader, img_rect, clip_rect):
        c = self.c
        c.saveState()
        cx, cy, cw, ch = clip_rect
        p = c.beginPath()
        p.rect(self.X(cx), self.Y(cy + ch), cw * PT, ch * PT)
        c.clipPath(p, stroke=0, fill=0)
        ix, iy, iw, ih = img_rect
        c.drawImage(reader, self.X(ix), self.Y(iy + ih), iw * PT, ih * PT, mask=None)
        c.restoreState()


def _begin_page(c, geo, profile):
    c.saveState()
    m = calibration.pdf_matrix(profile, geo["page_h"])
    if m:
        c.transform(*m)


def _end_page(c):
    c.restoreState()
    c.showPage()


def draw_cut_marks(pg: Page, geo: dict, layout: dict):
    if not layout["cut_marks"]:
        return
    g = geo["grid"]
    off = geo["outer_bleed"] + layout["cut_mark_gap"]
    ln = layout["cut_mark_len"]
    width, color = layout["cut_mark_width"], layout["cut_mark_color"]
    pw, ph = geo["page_w"], geo["page_h"]
    top, bottom = g["y"] - off, g["y"] + g["h"] + off
    left, right = g["x"] - off, g["x"] + g["w"] + off
    for x in geo["cuts_x"]:
        if top > 0:
            pg.line(x, top, x, max(0.0, top - ln) if ln else 0.0, width, color)
        if bottom < ph:
            pg.line(x, bottom, x, min(ph, bottom + ln) if ln else ph, width, color)
    for y in geo["cuts_y"]:
        if left > 0:
            pg.line(left, y, max(0.0, left - ln) if ln else 0.0, y, width, color)
        if right < pw:
            pg.line(right, y, min(pw, right + ln) if ln else pw, y, width, color)


def draw_label(pg: Page, geo: dict, layout: dict, text: str):
    if not layout["page_label"] or not text:
        return
    g = geo["grid"]
    avail_h = g["y"] - geo["outer_bleed"] - layout["cut_mark_gap"]
    if avail_h < 5:
        return
    x0 = g["x"] + 2
    x1 = geo["cuts_x"][1] - 2 if len(geo["cuts_x"]) > 1 else geo["page_w"] - 5
    pg.text(x0, avail_h / 2 + 2.5, text, size=6, color="#777777", max_w=x1 - x0)


class ImageCache:
    """Bereitet jedes Kartenbild einmal pro PDF auf; reportlab dedupliziert identische Bilder."""

    def __init__(self, cards: dict, layout: dict):
        self.cards = cards
        self.layout = layout
        self._cache: dict[str, tuple] = {}

    def get(self, card_id: str):
        if card_id in self._cache:
            return self._cache[card_id]
        card = self.cards.get(card_id)
        if not card:
            self._cache[card_id] = None
            return None
        path = config.CARDS_DIR / card["file"]
        if not path.exists():
            self._cache[card_id] = None
            return None
        q = self.layout["quality"]
        prep = imaging.prepare(path, card, max_dpi=imaging.QUALITY_MAX_DPI[q],
                               synthetic_bleed_mm=self.layout["synthetic_bleed"],
                               card_w_mm=self.layout["card_w"], card_h_mm=self.layout["card_h"])
        reader = ImageReader(imaging.encode_for_pdf(prep, q))
        entry = (reader, prep.fx, prep.fy)
        self._cache[card_id] = entry
        return entry


def draw_card(pg: Page, slot: dict, entry: tuple):
    reader, fx, fy = entry
    img_w = slot["w"] / (1 - 2 * fx)
    img_h = slot["h"] / (1 - 2 * fy)
    ax, ay = fx * img_w, fy * img_h  # verfügbarer Bleed (mm)
    b = slot["bleed"]
    l, r = min(b["l"], ax), min(b["r"], ax)
    t, btm = min(b["t"], ay), min(b["b"], ay)
    clip = (slot["x"] - l, slot["y"] - t, slot["w"] + l + r, slot["h"] + t + btm)
    pg.clip_image(reader, (slot["x"] - ax, slot["y"] - ay, img_w, img_h), clip)


def _new_canvas(path: Path, geo: dict, title: str) -> rl_canvas.Canvas:
    c = rl_canvas.Canvas(str(path), pagesize=(geo["page_w"] * PT, geo["page_h"] * PT))
    c.setTitle(title)
    c.setAuthor("MTG Print Studio")
    c.setCreator("MTG Print Studio")
    return c


def build_project_pdf(project: dict, cards: dict, out: Path, profile: dict | None = None,
                      pages: list[int] | None = None) -> dict:
    layout = normalize_layout(project.get("layout"))
    geo = geometry(layout)
    all_pages = project.get("pages", [])
    indices = pages if pages is not None else list(range(len(all_pages)))
    indices = [i for i in indices if 0 <= i < len(all_pages)]
    c = _new_canvas(out, geo, project.get("name", "MTG Proxies"))
    cache = ImageCache(cards, layout)
    missing = set()
    n_cards = 0
    for n, pi in enumerate(indices, start=1):
        pg = Page(c, geo["page_h"])
        _begin_page(c, geo, profile)
        for slot, card_id in zip(geo["slots"], all_pages[pi]):
            if not card_id:
                continue
            entry = cache.get(card_id)
            if entry is None:
                missing.add(card_id)
                continue
            draw_card(pg, slot, entry)
            n_cards += 1
        draw_cut_marks(pg, geo, layout)
        label = f"{project.get('name', '')} · Seite {pi + 1}/{len(all_pages)}"
        if profile and profile.get("id") != "default":
            label += f" · Kalibrierung: {profile.get('name', '')}"
        draw_label(pg, geo, layout, label)
        _end_page(c)
    if not indices:
        pg = Page(c, geo["page_h"])
        pg.text(geo["page_w"] / 2, 40, "Keine Seiten ausgewählt", 14, align="center")
        c.showPage()
    c.save()
    return {"pages": len(indices), "cards": n_cards, "missing": sorted(missing)}


# ---------------------------------------------------------------------------
# Kalibrierseite
# ---------------------------------------------------------------------------
def build_calibration_pdf(out: Path, profile: dict | None, paper: str = "A4") -> None:
    layout = normalize_layout({"paper": paper})
    geo = geometry(layout)
    pw, ph = geo["page_w"], geo["page_h"]
    ref = calibration.REF
    c = _new_canvas(out, geo, "Kalibrierseite")
    pg = Page(c, ph)
    _begin_page(c, geo, profile)

    lw = 0.15
    # Messlinien (über fast das ganze Blatt)
    pg.line(ref["A"], 8, ref["A"], ph - 8, lw)
    pg.line(ref["B"], 8, ref["B"], ph - 8, lw)
    pg.line(8, ref["C"], pw - 8, ref["C"], lw)
    pg.line(8, ref["D"], pw - 8, ref["D"], lw)
    for name, x in (("A", ref["A"]), ("B", ref["B"])):
        for y in (60, 150, 240):
            pg.text(x + 1.2, y, name, 11, font="Helvetica-Bold")
            pg.text(x + 1.2, y + 3.5, f"Soll {x:.0f} mm", 6, "#555555")
    for name, y in (("C", ref["C"]), ("D", ref["D"])):
        for x in (45, 105, 165):
            pg.text(x, y - 1.5, name, 11, font="Helvetica-Bold")
            pg.text(x + 4.5, y - 1.5, f"Soll {y:.0f} mm", 6, "#555555")

    # Lineal-Skalen (100 mm) zum Prüfen des Maßstabs mit einem eigenen Lineal
    x0, y0 = 55.0, 40.0
    pg.line(x0, y0, x0 + 100, y0, 0.2)
    for i in range(0, 101):
        h = 4 if i % 10 == 0 else (2.5 if i % 5 == 0 else 1.5)
        pg.line(x0 + i, y0, x0 + i, y0 + h, 0.12)
        if i % 10 == 0:
            pg.text(x0 + i, y0 + 7, str(i), 5.5, align="center")
    pg.text(x0 + 50, y0 - 2, "Horizontal 100 mm", 7, "#333333", align="center")

    x1, y1 = 35.0, 80.0
    pg.line(x1, y1, x1, y1 + 100, 0.2)
    for i in range(0, 101):
        w = 4 if i % 10 == 0 else (2.5 if i % 5 == 0 else 1.5)
        pg.line(x1, y1 + i, x1 + w, y1 + i, 0.12)
        if i % 10 == 0:
            pg.text(x1 + 5.5, y1 + i + 1.2, str(i), 5.5)
    pg.text(x1 - 1, y1 - 2.5, "Vertikal 100 mm", 7, "#333333")

    # Referenzkarte 63 x 88 mm
    cw, ch = config.DEFAULT_CARD_W_MM, config.DEFAULT_CARD_H_MM
    rx, ry = 95.0, 78.0
    pg.rect(rx, ry, cw, ch, stroke="#000000", fill="#f0f0f0", width=0.15)
    pg.text(rx + cw / 2, ry + ch / 2 - 2, "Referenzkarte", 9, align="center", font="Helvetica-Bold")
    pg.text(rx + cw / 2, ry + ch / 2 + 3, f"{cw:.0f} × {ch:.0f} mm", 9, align="center")
    pg.text(rx + cw / 2, ry + ch / 2 + 8, "Echte Karte drauflegen", 7, "#555555", align="center")

    # Anleitung
    ty = 194.0
    lines = [
        ("Kalibrierung – so geht's", 11, "Helvetica-Bold"),
        ("1. Diese Seite mit denselben Einstellungen wie die Kartenbögen gedruckt? (Papier, 100 %)", 8, "Helvetica"),
        ("2. Mit einem Stahllineal von der Papierkante bis zur MITTE der Linie messen:", 8, "Helvetica"),
        ("     A und B: von der LINKEN Kante,   C und D: von der OBEREN Kante", 8, "Helvetica"),
        ("3. Am besten jede Linie an zwei Stellen messen und den Mittelwert nehmen.", 8, "Helvetica"),
        ("4. Werte in der Web-Oberfläche unter 'Kalibrierung' eintragen und speichern.", 8, "Helvetica"),
        ("5. Danach die Probeschnitt-Seite drucken, schneiden und mit einer echten Karte vergleichen.", 8, "Helvetica"),
    ]
    for s, size, font in lines:
        pg.text(26, ty, s, size, font=font, max_w=160)
        ty += size * 0.55 + 2

    stamp = dt.datetime.now().strftime("%d.%m.%Y %H:%M")
    if profile:
        info = (f"Gedruckt mit Profil '{profile.get('name')}': Versatz X {profile.get('offset_x', 0):+.2f} mm, "
                f"Y {profile.get('offset_y', 0):+.2f} mm, Maßstab X {profile.get('scale_x', 1) * 100:.3f} %, "
                f"Y {profile.get('scale_y', 1) * 100:.3f} %")
    else:
        info = "Gedruckt OHNE Kalibrierung"
    pg.text(pw / 2, 268, info, 6.5, "#444444", align="center", max_w=160)
    pg.text(pw / 2, 271.5, f"MTG Print Studio · {stamp}", 6, "#777777", align="center")
    _end_page(c)
    c.save()


# ---------------------------------------------------------------------------
# Probeschnitt-Seite
# ---------------------------------------------------------------------------
def build_cut_test_pdf(out: Path, layout_data: dict | None, profile: dict | None) -> None:
    layout = normalize_layout(layout_data)
    geo = geometry(layout)
    c = _new_canvas(out, geo, "Probeschnitt")
    pg = Page(c, geo["page_h"])
    _begin_page(c, geo, profile)
    colors = ["#9ecae1", "#fdae6b"]
    cw, ch = layout["card_w"], layout["card_h"]
    for s in geo["slots"]:
        col = colors[(s["row"] + s["col"]) % 2]
        b = s["bleed"]
        pg.rect(s["x"] - b["l"], s["y"] - b["t"], s["w"] + b["l"] + b["r"], s["h"] + b["t"] + b["b"], fill=col)
    for s in geo["slots"]:
        x, y = s["x"], s["y"]
        # Innenrahmen 3 mm vom Rand: nach dem Schneiden müssen alle 4 Abstände gleich sein
        pg.rect(x + 3, y + 3, cw - 6, ch - 6, stroke="#333333", width=0.12)
        pg.line(x + cw / 2 - 4, y + ch / 2, x + cw / 2 + 4, y + ch / 2, 0.12, "#333333")
        pg.line(x + cw / 2, y + ch / 2 - 4, x + cw / 2, y + ch / 2 + 4, 0.12, "#333333")
        pg.text(x + cw / 2, y + 14, f"Slot {s['index'] + 1}", 10, "#222222", "Helvetica-Bold", "center")
        pg.text(x + cw / 2, y + 20, f"Soll: {cw:.1f} × {ch:.1f} mm", 7, "#222222", align="center")
        pg.text(x + cw / 2, y + ch - 14, "Innenrahmen überall 3 mm?", 6, "#333333", align="center")
        pg.text(x + cw / 2, y + ch - 10, "Fremde Farbe am Rand = Schnitt daneben", 6, "#333333", align="center")
    draw_cut_marks(pg, geo, layout)
    label = "Probeschnitt"
    if profile and profile.get("id") != "default":
        label += f" · Kalibrierung: {profile.get('name', '')}"
    draw_label(pg, geo, layout, label)
    _end_page(c)
    c.save()
