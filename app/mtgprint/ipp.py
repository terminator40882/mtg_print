"""Minimaler IPP-Client (nur Get-Printer-Attributes) für Status & Tintenstände.

Das eigentliche Drucken läuft über CUPS (treiberlos/IPP Everywhere). Dieser
Client fragt den Drucker direkt, damit die Web-Oberfläche Zustand, Tinte und
unterstützte Papiertypen anzeigen kann.
"""
from __future__ import annotations

import ssl
import struct
import urllib.parse
import urllib.request

TAG_OPERATION, TAG_END = 0x01, 0x03
INT_TAGS = {0x21, 0x23}
STRING_TAGS = {0x41, 0x42, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49}

PRINTER_STATES = {3: "idle", 4: "processing", 5: "stopped"}


def _attr(tag: int, name: str, value: bytes) -> bytes:
    n = name.encode()
    return struct.pack(">BH", tag, len(n)) + n + struct.pack(">H", len(value)) + value


def build_request(uri: str, request_id: int = 1, attrs=("all", "media-col-database")) -> bytes:
    body = struct.pack(">BBHI", 2, 0, 0x000B, request_id)
    body += bytes([TAG_OPERATION])
    body += _attr(0x47, "attributes-charset", b"utf-8")
    body += _attr(0x48, "attributes-natural-language", b"en")
    body += _attr(0x45, "printer-uri", uri.encode())
    first = True
    for a in attrs:
        body += _attr(0x44, "requested-attributes" if first else "", a.encode())
        first = False
    body += bytes([TAG_END])
    return body


def build_job_request(job_uri: str, request_id: int = 1, user: str = "root") -> bytes:
    """Get-Job-Attributes (0x0009) für einen Job der lokalen CUPS-Instanz."""
    body = struct.pack(">BBHI", 2, 0, 0x0009, request_id)
    body += bytes([TAG_OPERATION])
    body += _attr(0x47, "attributes-charset", b"utf-8")
    body += _attr(0x48, "attributes-natural-language", b"en")
    body += _attr(0x45, "job-uri", job_uri.encode())
    body += _attr(0x42, "requesting-user-name", user.encode())
    body += _attr(0x44, "requested-attributes", b"all")
    body += bytes([TAG_END])
    return body


JOB_STATES = {3: "wartend", 4: "angehalten", 5: "druckt", 6: "gestoppt",
              7: "abgebrochen", 8: "fehlgeschlagen", 9: "fertig"}


def get_local_job(job_num: int, timeout: float = 4.0) -> dict:
    """Status eines Jobs direkt aus dem lokalen CUPS (inkl. Gründen bei Abbruch)."""
    uri = f"ipp://localhost/jobs/{job_num}"
    req = urllib.request.Request(f"http://localhost:631/jobs/{job_num}", data=build_job_request(uri),
                                 method="POST", headers={"Content-Type": "application/ipp"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        status, attrs = parse_response(resp.read())
    if status >= 0x0400:
        raise RuntimeError(f"IPP-Fehler 0x{status:04x}")

    def one(k, d=None):
        v = attrs.get(k)
        return v[0] if v else d

    return {
        "state": JOB_STATES.get(one("job-state"), str(one("job-state"))),
        "state_code": one("job-state"),
        "reasons": attrs.get("job-state-reasons", []),
        "printer_message": one("job-printer-state-message", ""),
        "printer_reasons": attrs.get("job-printer-state-reasons", []),
        "impressions": one("job-impressions-completed"),
        "sheets": one("job-media-sheets-completed"),
        "k_octets": one("job-k-octets"),
        "format": one("document-format-supplied", one("document-format", "")),
        "name": one("job-name", ""),
    }


def parse_response(data: bytes) -> tuple[int, dict]:
    if len(data) < 8:
        raise ValueError("Antwort zu kurz")
    status = struct.unpack(">H", data[2:4])[0]
    pos = 8
    attrs: dict[str, list] = {}
    last = None
    depth = 0
    while pos < len(data):
        tag = data[pos]
        pos += 1
        if tag == TAG_END:
            break
        if tag < 0x10:  # Gruppen-Delimiter
            continue
        nlen = struct.unpack(">H", data[pos:pos + 2])[0]
        pos += 2
        name = data[pos:pos + nlen].decode("utf-8", "replace")
        pos += nlen
        vlen = struct.unpack(">H", data[pos:pos + 2])[0]
        pos += 2
        raw = data[pos:pos + vlen]
        pos += vlen
        if tag == 0x34:      # begCollection
            depth += 1
            if depth == 1:
                if name:
                    last = name
                attrs.setdefault(last, []).append("<collection>")
            continue
        if tag == 0x37:      # endCollection
            depth = max(0, depth - 1)
            continue
        if depth > 0:
            continue
        if tag in INT_TAGS and vlen == 4:
            value = struct.unpack(">i", raw)[0]
        elif tag == 0x22:
            value = bool(raw[0]) if raw else False
        elif tag == 0x33 and vlen == 8:
            value = struct.unpack(">ii", raw)
        elif tag == 0x32 and vlen == 9:
            x, y, u = struct.unpack(">iiB", raw)
            value = f"{x}x{y}{'dpi' if u == 3 else 'dpcm'}"
        elif tag in (0x35, 0x36):  # text/name WithLanguage
            ll = struct.unpack(">H", raw[:2])[0]
            tl = struct.unpack(">H", raw[2 + ll:4 + ll])[0]
            value = raw[4 + ll:4 + ll + tl].decode("utf-8", "replace")
        elif tag in STRING_TAGS:
            value = raw.decode("utf-8", "replace")
        else:
            value = raw.hex()
        if name:
            last = name
            attrs.setdefault(name, []).append(value)
        elif last:
            attrs[last].append(value)
    return status, attrs


def get_printer_attributes(uri: str, timeout: float = 6.0) -> dict:
    u = urllib.parse.urlparse(uri)
    scheme = "https" if u.scheme in ("ipps", "https") else "http"
    port = u.port or 631
    http_url = f"{scheme}://{u.hostname}:{port}{u.path or '/ipp/print'}"
    req = urllib.request.Request(http_url, data=build_request(uri), method="POST",
                                 headers={"Content-Type": "application/ipp"})
    ctx = None
    if scheme == "https":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # Drucker im LAN nutzen selbstsignierte Zertifikate
    # Direkt zum Drucker im LAN, nicht über einen evtl. gesetzten HTTP-Proxy
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         urllib.request.HTTPSHandler(context=ctx))
    with opener.open(req, timeout=timeout) as resp:
        data = resp.read()
    status, attrs = parse_response(data)
    if status >= 0x0400:
        raise RuntimeError(f"IPP-Fehler 0x{status:04x}")
    return attrs


def summarize(attrs: dict) -> dict:
    def one(k, d=None):
        v = attrs.get(k)
        return v[0] if v else d

    markers = []
    names = attrs.get("marker-names", [])
    levels = attrs.get("marker-levels", [])
    colors = attrs.get("marker-colors", [])
    types = attrs.get("marker-types", [])
    for i, n in enumerate(names):
        markers.append({
            "name": n,
            "level": levels[i] if i < len(levels) else -1,
            "color": colors[i] if i < len(colors) else "#888888",
            "type": types[i] if i < len(types) else "",
        })
    return {
        "make_and_model": one("printer-make-and-model", ""),
        "name": one("printer-name", ""),
        "state": PRINTER_STATES.get(one("printer-state"), "unknown"),
        "state_reasons": [r for r in attrs.get("printer-state-reasons", []) if r != "none"],
        "state_message": one("printer-state-message", ""),
        "markers": markers,
        "media_types": attrs.get("media-type-supported", []),
        "media_ready": attrs.get("media-ready", []),
        "media_sources": attrs.get("media-source-supported", []),
        "formats": attrs.get("document-format-supported", []),
        "qualities": attrs.get("print-quality-supported", []),
        "resolutions": attrs.get("printer-resolution-supported", []),
        "firmware": one("printer-firmware-string-version", ""),
    }
