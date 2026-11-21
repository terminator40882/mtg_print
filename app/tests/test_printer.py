import struct

from mtgprint import config, ipp, printer


def _opts(media):
    return [
        {"key": "PageSize", "label": "Media Size", "type": "PickOne", "default": "Letter",
         "choices": [{"value": v, "label": v} for v in ("Letter", "A4", "A4.Borderless", "4x6")]},
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
    rec = printer.recommended_options(_opts(canon))
    assert rec == {"MediaType": "PhotographicGlossy", "cupsPrintQuality": "High",
                   "PageSize": "A4", "ColorModel": "RGB"}
    # Explizites Glossy II (falls der Drucker es so meldet) hat Vorrang
    rec = printer.recommended_options(_opts(canon + ["ComCanonPhotoPaperPlusGlossyII"]))
    assert rec["MediaType"] == "ComCanonPhotoPaperPlusGlossyII"


def test_media_override_from_env(monkeypatch):
    monkeypatch.setattr(config, "PRINT_MEDIA_TYPE", "photographic-high-gloss")
    rec = printer.recommended_options(_opts(["Stationery", "PhotographicHighGloss", "PhotographicGlossy"]))
    assert rec["MediaType"] == "PhotographicHighGloss"


def test_never_borderless():
    rec = printer.recommended_options(_opts(["Stationery"]))
    assert rec["PageSize"] == "A4"


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
