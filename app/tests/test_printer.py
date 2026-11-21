import struct

import pytest

from mtgprint import config, ipp, printer


PT = 25.4 / 72
SIZES = [  # wie aus einer CUPS-PPD für einen Canon PIXMA (Maße in mm)
    {"name": "A4", "w": 210.0, "h": 297.0, "borderless": False},
    {"name": "A4.Borderless", "w": 210.0, "h": 297.0, "borderless": True},
    {"name": "5x7", "w": 127.0, "h": 177.8, "borderless": False},
    {"name": "5x7.Borderless", "w": 127.0, "h": 177.8, "borderless": True},
    {"name": "Letter", "w": 215.9, "h": 279.4, "borderless": False},
]


def _opts(media):
    return [
        {"key": "PageSize", "label": "Media Size", "type": "PickOne", "default": "Letter",
         "choices": [{"value": v["name"], "label": v["name"]} for v in SIZES]},
        {"key": "MediaType", "label": "Media Type", "type": "PickOne", "default": "Stationery",
         "choices": [{"value": v, "label": v} for v in media]},
        {"key": "cupsPrintQuality", "label": "Print Quality", "type": "PickOne", "default": "Normal",
         "choices": [{"value": v, "label": v} for v in ("Draft", "Normal", "High")]},
        {"key": "ColorModel", "label": "Output Mode", "type": "PickOne", "default": "RGB",
         "choices": [{"value": v, "label": v} for v in ("Gray", "RGB")]},
    ]


def test_recommend_glossy_ii_high_a4(monkeypatch):
    monkeypatch.setattr(config, "PRINT_MEDIA_TYPE", "auto")
    monkeypatch.setattr(config, "PRINT_QUALITY", "high")
    canon = ["Stationery", "PhotographicHighGloss", "Photographic", "PhotographicGlossy",
             "PhotographicSemiGloss", "PhotographicMatte", "Envelope"]
    rec = printer.recommended_options(_opts(canon), sizes=SIZES)
    assert rec == {"MediaType": "PhotographicGlossy", "cupsPrintQuality": "High",
                   "PageSize": "A4", "ColorModel": "RGB"}
    # Explizites Glossy II (falls der Drucker es so meldet) hat Vorrang
    rec = printer.recommended_options(_opts(canon + ["ComCanonPhotoPaperPlusGlossyII"]), sizes=SIZES)
    assert rec["MediaType"] == "ComCanonPhotoPaperPlusGlossyII"


def test_media_override_from_env(monkeypatch):
    monkeypatch.setattr(config, "PRINT_MEDIA_TYPE", "photographic-high-gloss")
    rec = printer.recommended_options(_opts(["Stationery", "PhotographicHighGloss", "PhotographicGlossy"]))
    assert rec["MediaType"] == "PhotographicHighGloss"


def test_page_size_follows_paper():
    assert printer.recommended_options(_opts(["Stationery"]), sizes=SIZES)["PageSize"] == "A4"
    assert printer.page_size_for("A4", False, SIZES) == "A4"
    assert printer.page_size_for("13x18", True, SIZES) == "5x7.Borderless"
    assert printer.page_size_for("13x18", False, SIZES) == "5x7"
    assert printer.page_size_for("13x18", True, [s for s in SIZES if s["name"] != "5x7.Borderless"]) is None
    assert printer.page_size_for("13x18", False, [s for s in SIZES if "5x7" not in s["name"]]) is None


def test_ppd_page_size_parsing(tmp_path, monkeypatch):
    ppd = tmp_path / "Q.ppd"
    ppd.write_text('''*PaperDimension 5x7/5 x 7": "360 504"
*ImageableArea 5x7/5 x 7": "9.64 14.17 350.36 489.83"
*PaperDimension 5x7.Borderless/5 x 7" (Borderless): "360 504"
*ImageableArea 5x7.Borderless/5 x 7" (Borderless): "0 0 360 504"
''')
    monkeypatch.setattr(printer, "ppd_path", lambda: ppd)
    sizes = {s["name"]: s for s in printer.ppd_page_sizes()}
    assert sizes["5x7"]["w"] == pytest.approx(127.0) and not sizes["5x7"]["borderless"]
    assert sizes["5x7.Borderless"]["borderless"]


def _attr(tag, name, value):
    n = name.encode()
    return struct.pack(">BH", tag, len(n)) + n + struct.pack(">H", len(value)) + value


def test_ipp_parse_markers_and_collections():
    body = struct.pack(">BBHI", 2, 0, 0, 1) + bytes([0x04])
    body += _attr(0x41, "printer-make-and-model", b"Canon G600 series")
    body += _attr(0x23, "printer-state", struct.pack(">i", 3))
    body += _attr(0x42, "marker-names", b"Black") + _attr(0x42, "", b"Cyan")
    body += _attr(0x21, "marker-levels", struct.pack(">i", 80)) + _attr(0x21, "", struct.pack(">i", 15))
    body += _attr(0x34, "media-col-database", b"") + _attr(0x4A, "", b"media-type") \
        + _attr(0x44, "", b"photographic") + _attr(0x37, "", b"")
    body += _attr(0x44, "media-type-supported", b"stationery") + _attr(0x44, "", b"photographic-glossy")
    body += bytes([0x03])
    status, attrs = ipp.parse_response(body)
    s = ipp.summarize(attrs)
    assert status == 0
    assert s["state"] == "idle"
    assert s["markers"][1] == {"name": "Cyan", "level": 15, "color": "#888888", "type": ""}
    assert s["media_types"] == ["stationery", "photographic-glossy"]
    assert attrs["media-col-database"] == ["<collection>"]


def test_build_request_roundtrip():
    req = ipp.build_request("ipp://192.168.1.5/ipp/print")
    assert req[:4] == bytes([2, 0, 0, 0x0B])
    assert b"printer-uri" in req and req.endswith(bytes([0x03]))


def test_pwg_size_and_paper_check(monkeypatch):
    assert ipp.pwg_size_mm("na_5x7_5x7in") == pytest.approx((127.0, 177.8))
    assert ipp.pwg_size_mm("jpn_photo-2l_127x177.8mm") == pytest.approx((127.0, 177.8))
    assert ipp.pwg_size_mm("iso_a4_210x297mm") == (210.0, 297.0)
    assert ipp.pwg_size_mm("auto") is None
    monkeypatch.setattr(printer, "printer_attributes", lambda: ({"media_ready": ["iso_a4_210x297mm"]}, ""))
    chk = printer.paper_check("13x18")
    assert chk["known"] and not chk["match"]
    monkeypatch.setattr(printer, "printer_attributes", lambda: ({"media_ready": ["na_5x7_5x7in"]}, ""))
    assert printer.paper_check("13x18")["match"]
