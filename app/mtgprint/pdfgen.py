"""PDF-Erzeugung: Kartenbögen, Kalibrierseite und Probeschnitt-Seite."""
from __future__ import annotations

import datetime as dt
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from reportlab import rl_config

from reportlab.lib.colors import Color, HexColor
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas as rl_canvas

from . import calibration, config, imaging
from .layout import geometry, normalize_layout

PT = 72.0 / 25.4  # PDF-Punkte pro mm

# Bilder binär einbetten statt als ASCII85-Text: reportlab kodiert ASCII85 ohne
# C-Beschleuniger in reinem Python – das war >80 % der gesamten PDF-Zeit.
rl_config.useA85 = 0


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

    def clip_image_rotated(self, reader, center, iw, ih, clip_rect):
        """Bild (hochkant iw x ih) um 90° gegen den Uhrzeigersinn gedreht um center zeichnen."""
        c = self.c
        c.saveState()
        cx, cy, cw, ch = clip_rect
        p = c.beginPath()
        p.rect(self.X(cx), self.Y(cy + ch), cw * PT, ch * PT)
        c.clipPath(p, stroke=0, fill=0)
        c.translate(self.X(center[0]), self.Y(center[1]))
        c.rotate(90)
        c.drawImage(reader, -iw / 2 * PT, -ih / 2 * PT, iw * PT, ih * PT, mask=None)
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

    def edge_tick(x1, y1, x2, y2):
        # Liegt über dem Bild-Bleed (randlos): weiß hinterlegt, damit sie auf jedem Motiv sichtbar ist
        if abs(x2 - x1) + abs(y2 - y1) < 0.3:
            return
        pg.line(x1, y1, x2, y2, width * 3, "#ffffff")
        pg.line(x1, y1, x2, y2, width, color)

    for x in geo["cuts_x"]:
        if top > 0:
            pg.line(x, top, x, max(0.0, top - ln) if ln else 0.0, width, color)
        else:
            edge_tick(x, 0.0, x, max(g["y"], 0.0))
        if bottom < ph:
            pg.line(x, bottom, x, min(ph, bottom + ln) if ln else ph, width, color)
        else:
            edge_tick(x, ph, x, min(g["y"] + g["h"], ph))
    for y in geo["cuts_y"]:
        if left > 0:
            pg.line(left, y, max(0.0, left - ln) if ln else 0.0, y, width, color)
        else:
            edge_tick(0.0, y, max(g["x"], 0.0), y)
        if right < pw:
            pg.line(right, y, min(pw, right + ln) if ln else pw, y, width, color)
        else:
            edge_tick(pw, y, min(g["x"] + g["w"], pw), y)


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
    """Bereitet jedes Kartenbild einmal pro PDF auf – parallel auf allen Kernen.

    Pillow gibt beim Dekodieren, Skalieren und Kodieren den GIL frei, daher
    reichen Threads. reportlab dedupliziert identische Bilder im PDF.
    """

    def __init__(self, cards: dict, layout: dict, max_dpi: float | None = None):
        self.cards = cards
        self.layout = layout
        q = layout["quality"]
        dpi = imaging.QUALITY_MAX_DPI[q]
        if max_dpi:
            dpi = min(dpi, max_dpi) if dpi else max_dpi
        self.max_dpi = dpi
        self._cache: dict[str, tuple | None] = {}

    def _load(self, card_id: str):
        card = self.cards.get(card_id)
        if not card:
            return None
        path = config.CARDS_DIR / card["file"]
        if not path.exists():
            return None
        data, fx, fy = imaging.pdf_image(path, card, self.layout["quality"], self.max_dpi,
                                         self.layout["synthetic_bleed"], self.layout["card_w"],
                                         self.layout["card_h"])
        src = str(data) if isinstance(data, Path) else ImageReader(io.BytesIO(data))
        return src, fx, fy

    def prefetch(self, card_ids) -> None:
        todo = [c for c in dict.fromkeys(card_ids) if c and c not in self._cache]
        if not todo:
            return
        with ThreadPoolExecutor(max_workers=min(config.WORKERS, len(todo))) as ex:
            for cid, entry in zip(todo, ex.map(self._load, todo)):
                self._cache[cid] = entry

    def get(self, card_id: str):
        if card_id not in self._cache:
            self._cache[card_id] = self._load(card_id)
        return self._cache[card_id]


def draw_card(pg: Page, slot: dict, entry: tuple):
    reader, fx, fy = entry
    rotated = slot.get("rotated")
    # Abmessungen der Karte im Bild (immer hochkant)
    card_w, card_h = (slot["h"], slot["w"]) if rotated else (slot["w"], slot["h"])
    img_w = card_w / (1 - 2 * fx)
    img_h = card_h / (1 - 2 * fy)
    ax, ay = fx * img_w, fy * img_h  # verfügbarer Bleed (mm) im Bild
    if rotated:
        ax, ay = ay, ax               # auf dem Blatt sind die Achsen vertauscht
    b = slot["bleed"]
    l, r = min(b["l"], ax), min(b["r"], ax)
    t, btm = min(b["t"], ay), min(b["b"], ay)
    clip = (slot["x"] - l, slot["y"] - t, slot["w"] + l + r, slot["h"] + t + btm)
    if rotated:
        center = (slot["x"] + slot["w"] / 2, slot["y"] + slot["h"] / 2)
        pg.clip_image_rotated(reader, center, img_w, img_h, clip)
    else:
        pg.clip_image(reader, (slot["x"] - ax, slot["y"] - ay, img_w, img_h), clip)


def _new_canvas(path: Path, geo: dict, title: str) -> rl_canvas.Canvas:
    c = rl_canvas.Canvas(str(path), pagesize=(geo["page_w"] * PT, geo["page_h"] * PT))
    c.setTitle(title)
    c.setAuthor("MTG Print Studio")
    c.setCreator("MTG Print Studio")
    return c


def build_project_pdf(project: dict, cards: dict, out: Path, profile: dict | None = None,
                      pages: list[int] | None = None, max_dpi: float | None = None) -> dict:
    """max_dpi: zusätzliche Obergrenze, z.B. die Rasterauflösung des Druckers beim Drucken."""
    layout = normalize_layout(project.get("layout"))
    geo = geometry(layout)
    all_pages = project.get("pages", [])
    indices = pages if pages is not None else list(range(len(all_pages)))
    indices = [i for i in indices if 0 <= i < len(all_pages)]
    c = _new_canvas(out, geo, project.get("name", "MTG Proxies"))
    cache = ImageCache(cards, layout, max_dpi)
    cache.prefetch(s for pi in indices for s in all_pages[pi])
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
    imaging.prune_cache()
    return {"pages": len(indices), "cards": n_cards, "missing": sorted(missing)}


# ---------------------------------------------------------------------------
# Kalibrierseite
# ---------------------------------------------------------------------------
def build_calibration_pdf(out: Path, profile: dict | None, paper: str = "A4", borderless: bool = False) -> None:
    layout = normalize_layout({"paper": paper, "borderless": borderless})
    geo = geometry(layout)
    pw, ph = geo["page_w"], geo["page_h"]
    ref = calibration.ref_for(paper)
    small = pw < 180
    c = _new_canvas(out, geo, "Kalibrierseite")
    pg = Page(c, ph)
    _begin_page(c, geo, profile)

    lw = 0.15
    edge = 5 if small else 8
    # Messlinien (über fast das ganze Blatt)
    pg.line(ref["A"], edge, ref["A"], ph - edge, lw)
    pg.line(ref["B"], edge, ref["B"], ph - edge, lw)
    pg.line(edge, ref["C"], pw - edge, ref["C"], lw)
    pg.line(edge, ref["D"], pw - edge, ref["D"], lw)
    ys = (45, ph / 2, ph - 45) if not small else (ref["C"] + 12, ref["D"] - 8)
    xs = (45, pw / 2, pw - 45) if not small else (pw / 2 - 10,)
    for name, x in (("A", ref["A"]), ("B", ref["B"])):
        for y in ys:
            pg.text(x + 1.2, y, name, 11, font="Helvetica-Bold")
            pg.text(x + 1.2, y + 3.5, f"Soll {x:.0f} mm", 6, "#555555")
    for name, y in (("C", ref["C"]), ("D", ref["D"])):
        for x in xs:
            pg.text(x, y - 1.5, name, 11, font="Helvetica-Bold")
            pg.text(x + 4.5, y - 1.5, f"Soll {y:.1f} mm".replace(".0 ", " "), 6, "#555555")

    cw, ch = config.DEFAULT_CARD_W_MM, config.DEFAULT_CARD_H_MM
    if not small:
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
        rx, ry = 95.0, 78.0
    else:
        rx, ry = (pw - cw) / 2, (ref["C"] + ref["D"]) / 2 - ch / 2
    pg.rect(rx, ry, cw, ch, stroke="#000000", fill="#f0f0f0", width=0.15)
    pg.text(rx + cw / 2, ry + ch / 2 - 2, "Referenzkarte", 9, align="center", font="Helvetica-Bold")
    pg.text(rx + cw / 2, ry + ch / 2 + 3, f"{cw:.0f} × {ch:.0f} mm", 9, align="center")
    pg.text(rx + cw / 2, ry + ch / 2 + 8, "Echte Karte drauflegen", 7, "#555555", align="center")

    stamp = dt.datetime.now().strftime("%d.%m.%Y %H:%M")
    if profile:
        info = (f"Profil '{profile.get('name')}': X {profile.get('offset_x', 0):+.2f} mm, "
                f"Y {profile.get('offset_y', 0):+.2f} mm, {profile.get('scale_x', 1) * 100:.3f} % / "
                f"{profile.get('scale_y', 1) * 100:.3f} %")
    else:
        info = "Gedruckt OHNE Kalibrierung"
    paper_txt = config.PAPER_LABELS.get(paper, paper) + (" randlos" if borderless else "")
    if not small:
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
        for txt, size, font in lines:
            pg.text(26, ty, txt, size, font=font, max_w=160)
            ty += size * 0.55 + 2
        pg.text(pw / 2, 268, info, 6.5, "#444444", align="center", max_w=160)
        pg.text(pw / 2, 271.5, f"{paper_txt} · MTG Print Studio · {stamp}", 6, "#777777", align="center")
    else:
        pg.text(pw / 2, ref["C"] + 6, f"Kalibrierung {paper_txt}", 8, font="Helvetica-Bold", align="center",
                max_w=ref["B"] - ref["A"] - 6)
        pg.text(pw / 2, ry - 3, "A/B von LINKER, C/D von OBERER Kante messen", 5.5, "#333333", align="center",
                max_w=ref["B"] - ref["A"] - 6)
        pg.text(pw / 2, ry + ch + 5, info, 4.8, "#444444", align="center", max_w=ref["B"] - ref["A"] - 4)
        pg.text(pw / 2, ry + ch + 8.5, stamp, 4.8, "#777777", align="center")
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
    cw, ch = geo["slots"][0]["w"], geo["slots"][0]["h"]
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
        pg.text(x + cw / 2, y + ch - 14, "Innenrahmen überall 3 mm?", 6, "#333333", align="center", max_w=cw - 8)
        pg.text(x + cw / 2, y + ch - 10, "Fremde Farbe am Rand = Schnitt daneben", 6, "#333333", align="center",
                max_w=cw - 8)
    draw_cut_marks(pg, geo, layout)
    label = "Probeschnitt"
    if profile and profile.get("id") != "default":
        label += f" · Kalibrierung: {profile.get('name', '')}"
    draw_label(pg, geo, layout, label)
    _end_page(c)
    c.save()
