import io

import pytest
from PIL import Image

from mtgprint import calibration, imaging, layout, pdfgen


def test_default_grid_is_3x3_with_8_cuts():
    geo = layout.geometry({})
    assert (geo["cols"], geo["rows"]) == (3, 3)
    assert geo["num_cuts"] == 8
    assert geo["cuts_x"] == [10.5, 73.5, 136.5, 199.5]
    assert geo["cuts_y"] == [16.5, 104.5, 192.5, 280.5]
    assert geo["fits"]
    # Karten stoßen exakt aneinander
    s0, s1 = geo["slots"][0], geo["slots"][1]
    assert s0["x"] + s0["w"] == pytest.approx(s1["x"])
    assert s0["bleed"]["l"] == 3.0 and s0["bleed"]["r"] == 0.0


def test_gap_creates_double_cuts():
    geo = layout.geometry({"gap": 2})
    assert geo["num_cuts"] == 12
    assert geo["slots"][0]["bleed"]["r"] == 1.0


def test_reflow_keeps_order():
    pages = [["a", None, "b"], ["c", None, None]]
    assert layout.reflow(pages, 2) == [["a", None], ["b", "c"]]


def test_mpc_bleed_fractions_match_template():
    fx, fy = imaging.bleed_fractions("mpc", 822, 1122)
    assert fx * 822 == pytest.approx(36, abs=0.05)   # 0.12" * 300 dpi
    assert fy * 1122 == pytest.approx(36, abs=0.05)


def test_auto_detect():
    assert imaging.detect_bleed_inches(822, 1122) == pytest.approx(0.12, abs=0.001)
    assert imaging.suggest_mode(822, 1122) == "mpc"
    assert imaging.suggest_mode(745, 1040) == "none"
    assert imaging.bleed_fractions("auto", 745, 1040) == (0.0, 0.0)


@pytest.mark.parametrize("w,h,name,bleed_px", [
    (3288, 4488, "MPC-Vorlage", 144.0),   # exakt 1200 DPI
    (2192, 2992, "MPC-Vorlage", 96.0),    # exakt 800 DPI (Horned Loch-Whale)
    (822, 1122, "MPC-Vorlage", 36.0),     # MPC-Template 300 DPI
    (2176, 2960, "63×88 mm + 1/8", 99.6),
    (2187, 2975, "63×88 mm + 1/8", 100.1),
    (2188, 2980, "MPC-Fill", 96.6),
])
def test_convention_detection_real_images(w, h, name, bleed_px):
    """Bildgrößen aus echten MPC-Fill-Downloads des Nutzers."""
    m = imaging.match_convention(w, h)
    assert m["name"].startswith(name) or name in m["name"]
    fx, fy = imaging.bleed_fractions("mpc", w, h)
    assert fx * w == pytest.approx(bleed_px, abs=0.5)
    # gleicher physischer Bleed auf beiden Achsen -> gleich viele Pixel
    assert fy * h == pytest.approx(fx * w, rel=0.005)


def _png(w, h, color=(200, 30, 30), alpha=False):
    im = Image.new("RGBA" if alpha else "RGB", (w, h), color + ((255,) if alpha else ()))
    if alpha:
        for x in range(10):
            for y in range(10):
                im.putpixel((x, y), (0, 0, 0, 0))
    return im


def test_prepare_synthetic_bleed_and_corner_fill(tmp_path):
    p = tmp_path / "c.png"
    im = _png(745, 1040, (10, 10, 10), alpha=True)
    im.save(p)
    prep = imaging.prepare(p, {"bleed_mode": "none", "fill_corners": True})
    assert prep.synthetic
    w, h = prep.image.size
    card_w = w * (1 - 2 * prep.fx)
    assert card_w == pytest.approx(745, abs=1)
    # Transparente Ecke mit Randfarbe gefüllt (nicht weiß)
    px = prep.image.getpixel((round(prep.fx * w) + 2, round(prep.fy * h) + 2))
    assert max(px) < 40


def test_prepare_downscale(tmp_path):
    p = tmp_path / "big.png"
    _png(2192, 2992).save(p)  # ~800 DPI MPC
    prep = imaging.prepare(p, {"bleed_mode": "mpc"}, max_dpi=600)
    dpi = prep.card_px[0] / (63 / 25.4)
    assert dpi == pytest.approx(600, rel=0.01)


def test_calibration_roundtrip():
    # Drucker: 0.8 mm zu weit rechts, 1.2 mm zu weit oben, 99.5 % / 100.3 %
    S = {"x": 0.995, "y": 1.003}
    O = {"x": 0.8, "y": -1.2}
    ref = calibration.REF
    meas = lambda v, ax: S[ax] * v + O[ax]
    m = calibration.from_measurements(meas(ref["A"], "x"), meas(ref["B"], "x"),
                                      meas(ref["C"], "y"), meas(ref["D"], "y"))
    assert m["scale_x"] == pytest.approx(0.995)
    assert m["offset_y"] == pytest.approx(-1.2)
    # Zweite Runde: mit dem Profil gedruckt -> Restfehler 0 -> Ergebnis bleibt gleich
    prof = {**calibration.DEFAULT_PROFILE, **m}
    # Was der Drucker jetzt druckt: S * pdf + O, pdf = (t - o)/s
    meas2 = lambda t, ax: S[ax] * (t - prof[f"offset_{ax}"]) / prof[f"scale_{ax}"] + O[ax]
    m2 = calibration.from_measurements(meas2(ref["A"], "x"), meas2(ref["B"], "x"),
                                       meas2(ref["C"], "y"), meas2(ref["D"], "y"))
    assert m2["scale_x"] == pytest.approx(1.0, abs=1e-5)
    assert m2["offset_x"] == pytest.approx(0.0, abs=1e-3)
    total = calibration.combine(prof, m2)
    assert total["offset_x"] == pytest.approx(0.8, abs=1e-3)
    assert total["scale_y"] == pytest.approx(1.003, abs=1e-5)


def test_pdf_matrix_maps_targets():
    prof = {"offset_x": 1.0, "offset_y": -2.0, "scale_x": 1.01, "scale_y": 0.99}
    a, b, c, d, e, f = calibration.pdf_matrix(prof, 297)
    pt = 72 / 25.4
    # Soll-Punkt 50 mm von links / 100 mm von oben
    x_pdf = (a * 50 * pt + e) / pt
    y_pdf_top = 297 - (d * (297 - 100) * pt + f) / pt
    # Drucker: printed = s * pdf + o (von oben gemessen)
    assert 1.01 * x_pdf + 1.0 == pytest.approx(50)
    assert 0.99 * y_pdf_top - 2.0 == pytest.approx(100)


def test_unplausible_measurement():
    with pytest.raises(ValueError):
        calibration.from_measurements(20, 100, 20, 277)


def test_pdf_builds(tmp_path):
    from mtgprint import config
    p = config.CARDS_DIR / "x.png"
    _png(822, 1122).save(p)
    cards = {"x": {"id": "x", "file": "x.png", "bleed_mode": "mpc"}}
    project = {"name": "Test", "layout": {}, "pages": [["x"] * 9, ["x", None] + [None] * 7]}
    out = tmp_path / "o.pdf"
    res = pdfgen.build_project_pdf(project, cards, out, calibration.DEFAULT_PROFILE)
    assert res == {"pages": 2, "cards": 10, "missing": []}
    data = out.read_bytes()
    assert data.startswith(b"%PDF")
    # Bild nur einmal eingebettet
    assert data.count(b"/Subtype /Image") == 1
    pdfgen.build_calibration_pdf(tmp_path / "cal.pdf", None)
    pdfgen.build_cut_test_pdf(tmp_path / "cut.pdf", {}, None)


def test_mpc_preset_matches_mpcfill_definition():
    """MPC-Fill (frontend/src/common/constants.ts): BleedEdgeMM = 0.12 in, Karte 63x88 mm.
    Unser Preset (MPC-Vorlage 2.74"x3.74") schneidet praktisch dieselbe Fläche aus."""
    w, h = 2192, 2992  # typisches MPC-Fill-Bild (~800 DPI)
    fx, fy = imaging.bleed_fractions("mpc", w, h)
    mpcfill_fx = 3.048 / (63 + 2 * 3.048)
    mpcfill_fy = 3.048 / (88 + 2 * 3.048)
    # Abweichung in mm auf der gedruckten Karte (63 mm Breite / 88 mm Höhe)
    assert abs(fx - mpcfill_fx) * 69.1 < 0.05
    assert abs(fy - mpcfill_fy) * 94.1 < 0.05


def test_pdf_image_passthrough_and_cache(tmp_path):
    from mtgprint import config
    p = config.CARDS_DIR / "j.jpg"
    _png(822, 1122).save(p, "JPEG", quality=90)
    card = {"bleed_mode": "mpc"}
    src, fx, fy = imaging.pdf_image(p, card, "high", 1200, 3.0, 63, 88)
    assert src == p  # Original-JPEG wird unverändert eingebettet
    # Drehung erzwingt Aufbereitung -> landet im Cache, zweiter Aufruf liefert dieselbe Datei
    card = {"bleed_mode": "mpc", "rotation": 180}
    src1, _, _ = imaging.pdf_image(p, card, "high", 1200, 3.0, 63, 88)
    src2, _, _ = imaging.pdf_image(p, card, "high", 1200, 3.0, 63, 88)
    assert src1 != p and src1 == src2 and src1.parent == config.CACHE_DIR
    # Druckauflösung kleiner als Bildauflösung -> herunterskaliert
    big = config.CARDS_DIR / "big.jpg"
    _png(3288, 4488).save(big, "JPEG")
    src3, _, _ = imaging.pdf_image(big, {"bleed_mode": "mpc"}, "high", 600, 3.0, 63, 88)
    from PIL import Image
    assert Image.open(src3).size[0] == pytest.approx(3288 / 2, abs=2)
