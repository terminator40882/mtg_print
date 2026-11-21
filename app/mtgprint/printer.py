"""Drucker-Anbindung über CUPS mit treiberloser Queue (IPP Everywhere / AirPrint).

Die Queue wird beim Start automatisch aus PRINTER_IP / PRINTER_URI angelegt
(``lpadmin -m everywhere``). CUPS erzeugt daraus eine PPD mit allen Optionen,
die der Drucker per IPP meldet (Papiertyp, Qualität, Papierzufuhr, ...).
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import config, ipp

log = logging.getLogger("mtgprint.printer")

PPD_DIR = Path("/etc/cups/ppd")

_state = {
    "queue_ready": False,
    "last_error": "",
    "last_attempt": 0.0,
    "setting_up": False,
}
_attr_cache = {"t": 0.0, "data": None, "error": ""}
_lock = threading.Lock()


def _run(cmd: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    log.debug("run: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def cups_available() -> bool:
    return shutil.which("lpadmin") is not None and shutil.which("lp") is not None


def cups_running() -> bool:
    if not cups_available():
        return False
    try:
        r = _run(["lpstat", "-r"], timeout=5)
        return "is running" in r.stdout or "läuft" in r.stdout
    except Exception:
        return False


def start_cups() -> None:
    if cups_available() and not cups_running() and shutil.which("cupsd"):
        subprocess.Popen(["cupsd"])
        for _ in range(20):
            if cups_running():
                return
            time.sleep(0.25)


def configured() -> bool:
    return bool(config.printer_uri())


def queue_exists() -> bool:
    try:
        r = _run(["lpstat", "-p", config.PRINTER_QUEUE], timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def ppd_path() -> Path:
    return PPD_DIR / f"{config.PRINTER_QUEUE}.ppd"


def queue_valid() -> bool:
    """Queue existiert UND hat eine treiberlos erzeugte PPD.

    Ist der Drucker beim Anlegen nicht erreichbar, legt CUPS u.U. eine Queue
    ohne PPD an, die Daten roh weiterreichen würde – die ist unbrauchbar.
    """
    return queue_exists() and ppd_path().exists()


def setup_queue(force: bool = False) -> dict:
    """Legt die treiberlose Queue an (bzw. neu an)."""
    uri = config.printer_uri()
    if not uri:
        _state.update(queue_ready=False, last_error="Kein Drucker konfiguriert (PRINTER_IP in .env setzen)")
        return status_basic()
    if not cups_available():
        _state.update(queue_ready=False, last_error="CUPS ist nicht installiert (läuft die App außerhalb von Docker?)")
        return status_basic()
    with _lock:
        _state["setting_up"] = True
        _state["last_attempt"] = time.time()
        try:
            start_cups()
            if queue_valid() and not force:
                _state.update(queue_ready=True, last_error="")
                return {**status_basic(), "setting_up": False}
            if queue_exists():
                _run(["lpadmin", "-x", config.PRINTER_QUEUE])
            r = _run(["lpadmin", "-p", config.PRINTER_QUEUE, "-E", "-v", uri, "-m", "everywhere",
                      "-D", "Canon G650 (treiberlos)", "-L", "MTG Print Studio"], timeout=90)
            # CUPS 2.4 erzeugt die PPD asynchron im Hintergrund -> kurz warten
            if r.returncode == 0:
                for _ in range(90):
                    if ppd_path().exists():
                        break
                    time.sleep(0.5)
            if r.returncode != 0 or not ppd_path().exists():
                msg = (r.stderr or r.stdout).strip() or "Drucker nicht erreichbar oder unterstützt kein IPP Everywhere/AirPrint"
                if queue_exists():
                    _run(["lpadmin", "-x", config.PRINTER_QUEUE])
                _state.update(queue_ready=False, last_error=f"Queue konnte nicht angelegt werden: {msg}")
                log.warning("lpadmin fehlgeschlagen: %s", msg)
                return {**status_basic(), "setting_up": False}
            _run(["cupsenable", config.PRINTER_QUEUE])
            _run(["cupsaccept", config.PRINTER_QUEUE])
            _run(["lpadmin", "-d", config.PRINTER_QUEUE])
            # Sicherheitsnetz: niemals skalieren
            _run(["lpadmin", "-p", config.PRINTER_QUEUE, "-o", "print-scaling-default=none"])
            _state.update(queue_ready=True, last_error="")
            log.info("Druckerqueue %s für %s angelegt", config.PRINTER_QUEUE, uri)
        except subprocess.TimeoutExpired:
            _state.update(queue_ready=False, last_error="Zeitüberschreitung beim Kontaktieren des Druckers")
        except Exception as e:  # pragma: no cover
            _state.update(queue_ready=False, last_error=str(e))
        finally:
            _state["setting_up"] = False
    return status_basic()


def setup_loop() -> None:
    """Hintergrund-Thread: Queue anlegen, bei Fehlschlag regelmäßig neu versuchen."""
    if not configured():
        return
    delay = 5
    while True:
        if not _state["queue_ready"] or not queue_valid():
            _state["queue_ready"] = False
            setup_queue()
        if _state["queue_ready"]:
            delay = 60
        else:
            delay = min(delay * 2, 120)
        time.sleep(delay)


def status_basic() -> dict:
    return {
        "configured": configured(),
        "uri": config.printer_uri(),
        "queue": config.PRINTER_QUEUE,
        "queue_ready": _state["queue_ready"],
        "setting_up": _state["setting_up"],
        "error": _state["last_error"],
        "cups": cups_available(),
    }


def printer_attributes(max_age: float = 10.0) -> tuple[dict | None, str]:
    uri = config.printer_uri()
    if not uri:
        return None, "nicht konfiguriert"
    now = time.time()
    if _attr_cache["data"] is not None and now - _attr_cache["t"] < max_age:
        return _attr_cache["data"], ""
    if _attr_cache["error"] and now - _attr_cache["t"] < 5:
        return _attr_cache["data"], _attr_cache["error"]
    try:
        attrs = ipp.get_printer_attributes(uri)
        _attr_cache.update(t=now, data=ipp.summarize(attrs), error="")
    except Exception as e:
        _attr_cache.update(t=now, error=f"Drucker nicht erreichbar: {e}")
    return _attr_cache["data"], _attr_cache["error"]


def queue_state() -> str:
    try:
        r = _run(["lpstat", "-p", config.PRINTER_QUEUE], timeout=5)
        return r.stdout.strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# PPD-Optionen
# ---------------------------------------------------------------------------
def ppd_options() -> list[dict]:
    """Optionen der Queue inkl. lesbarer Bezeichnungen aus der PPD."""
    path = ppd_path()
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    opts: list[dict] = []
    current = None
    for line in text.splitlines():
        m = re.match(r"\*OpenUI \*([^/:]+)(?:/([^:]*))?:\s*(\w+)", line)
        if m:
            current = {"key": m.group(1), "label": (m.group(2) or m.group(1)).strip(),
                       "type": m.group(3), "choices": [], "default": None}
            opts.append(current)
            continue
        if line.startswith("*CloseUI"):
            current = None
            continue
        if current is None:
            m = re.match(r"\*Default([^:]+):\s*(\S+)", line)
            if m:
                for o in opts:
                    if o["key"] == m.group(1):
                        o["default"] = m.group(2)
            continue
        m = re.match(r"\*Default" + re.escape(current["key"]) + r":\s*(\S+)", line)
        if m:
            current["default"] = m.group(1)
            continue
        m = re.match(r"\*" + re.escape(current["key"]) + r"\s+([^/:\s]+)(?:/([^:]*))?:", line)
        if m:
            current["choices"].append({"value": m.group(1), "label": (m.group(2) or m.group(1)).strip()})
    hidden = {"PageRegion", "cupsFinishingTemplate"}
    return [o for o in opts if o["choices"] and o["key"] not in hidden]


def ppd_page_sizes() -> list[dict]:
    """Seitengrößen aus der PPD: Name, Maße (mm) und ob randlos (bedruckbarer Bereich = Blatt)."""
    path = ppd_path()
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    pt = 25.4 / 72
    dims = {m.group(1): (float(m.group(2)) * pt, float(m.group(3)) * pt)
            for m in re.finditer(r'\*PaperDimension ([^/:\s]+)[^:]*:\s*"([\d.]+)\s+([\d.]+)"', text)}
    areas = {m.group(1): tuple(float(m.group(i)) * pt for i in range(2, 6))
             for m in re.finditer(r'\*ImageableArea ([^/:\s]+)[^:]*:\s*"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)"', text)}
    out = []
    for name, (w, h) in dims.items():
        a = areas.get(name)
        borderless = bool(a) and a[0] < 0.3 and a[1] < 0.3 and a[2] > w - 0.3 and a[3] > h - 0.3
        out.append({"name": name, "w": w, "h": h, "borderless": borderless or "borderless" in name.lower()})
    return out


def page_size_for(paper: str, borderless: bool, sizes: list[dict] | None = None) -> str | None:
    """Passende PPD-Seitengröße für Papier (z.B. 13x18 -> '5x7' bzw. '5x7.Borderless')."""
    sizes = sizes if sizes is not None else ppd_page_sizes()
    pw, ph = config.PAPER_SIZES_MM.get(paper, config.PAPER_SIZES_MM["A4"])
    cands = [sz for sz in sizes if abs(sz["w"] - pw) < 1.6 and abs(sz["h"] - ph) < 1.6]
    if not cands:
        return None
    # streng: randlos nur mit randloser Variante, mit Rand nur mit normaler Variante –
    # sonst würden Außenkarten abgeschnitten bzw. das Bild vergrößert
    pick = [sz for sz in cands if sz["borderless"] == borderless]
    if not pick:
        return None
    # kürzester Name = Standardvariante (z.B. "A4" statt "A4.Fullbleed")
    return sorted(pick, key=lambda sz: len(sz["name"]))[0]["name"]


def paper_check(paper: str) -> dict:
    """Vergleicht das Layout-Papier mit dem, was der Drucker als eingelegt meldet (media-ready)."""
    attrs, _ = printer_attributes()
    loaded = []
    for name in (attrs or {}).get("media_ready", []):
        dims = ipp.pwg_size_mm(name)
        if dims:
            loaded.append({"name": name, "w": round(dims[0], 1), "h": round(dims[1], 1)})
    pw, ph = config.PAPER_SIZES_MM.get(paper, config.PAPER_SIZES_MM["A4"])
    match = any(abs(m["w"] - pw) < 1.6 and abs(m["h"] - ph) < 1.6 for m in loaded)
    return {"loaded": loaded, "match": match, "known": bool(loaded)}


def raster_dpi(quality_choice: str | None = None) -> int:
    """Rasterauflösung, die CUPS für die gewählte Qualität an den Drucker schickt."""
    path = ppd_path()
    if not path.exists():
        return 600
    text = path.read_text(encoding="utf-8", errors="replace")
    q = quality_choice or "High"
    m = re.search(r"\*cupsPrintQuality " + re.escape(q) + r"[^:]*:.*?HWResolution\[(\d+)\s+(\d+)\]", text, re.S)
    if not m:
        m = re.search(r"HWResolution\[(\d+)\s+(\d+)\]", text)
    if not m:
        m = re.search(r"\*DefaultResolution:\s*(\d+)", text)
        return int(m.group(1)) if m else 600
    return max(int(m.group(1)), int(m.group(2)))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _score_media(value: str, label: str) -> int:
    v = _norm(value) + "|" + _norm(label)
    if "glossy" in v and ("ii" in v or "2" in v or "plus" in v):
        return 100
    if "photographicglossy" in v:
        return 80
    if "glossy" in v and "semi" not in v:
        return 70
    if "highgloss" in v:
        return 60
    if "photo" in v and "matte" not in v and "semi" not in v:
        return 40
    return 0


def recommended_options(opts: list[dict] | None = None, paper: str = "A4", borderless: bool = False,
                        sizes: list[dict] | None = None) -> dict:
    """Empfohlene Optionen: Glossy II, hohe Qualität, Farbe, Seitengröße passend zum Papier
    (A4 nicht randlos; randlos nur, wenn das Layout es verlangt)."""
    opts = opts if opts is not None else ppd_options()
    rec: dict[str, str] = {}
    by_key = {o["key"]: o for o in opts}

    mt = by_key.get("MediaType")
    if mt:
        wanted = config.PRINT_MEDIA_TYPE
        choice = None
        if wanted.lower() != "auto":
            for c in mt["choices"]:
                if _norm(c["value"]) == _norm(wanted) or _norm(c["label"]) == _norm(wanted):
                    choice = c["value"]
        if not choice:
            best = max(mt["choices"], key=lambda c: _score_media(c["value"], c["label"]))
            if _score_media(best["value"], best["label"]) > 0:
                choice = best["value"]
        if choice:
            rec["MediaType"] = choice

    q = by_key.get("cupsPrintQuality")
    if q:
        wanted = {"high": "High", "normal": "Normal", "draft": "Draft"}.get(config.PRINT_QUALITY.lower(), "High")
        vals = [c["value"] for c in q["choices"]]
        rec["cupsPrintQuality"] = wanted if wanted in vals else vals[-1]

    ps = by_key.get("PageSize")
    if ps:
        name = page_size_for(paper, borderless, sizes)
        vals = [c["value"] for c in ps["choices"]]
        if name in vals:
            rec["PageSize"] = name

    cm = by_key.get("ColorModel")
    if cm:
        vals = [c["value"] for c in cm["choices"]]
        for cand in ("RGB", "AdobeRGB", "Color", "CMYK"):
            if cand in vals:
                rec["ColorModel"] = cand
                break

    src = by_key.get("InputSlot")
    if src and config.PRINT_INPUT_SLOT.lower() != "auto":
        for c in src["choices"]:
            if _norm(c["value"]) == _norm(config.PRINT_INPUT_SLOT):
                rec["InputSlot"] = c["value"]

    dup = by_key.get("Duplex")
    if dup and "None" in [c["value"] for c in dup["choices"]]:
        rec["Duplex"] = "None"
    return rec


def effective_options(saved: dict | None, paper: str = "A4", borderless: bool = False) -> dict:
    """Empfehlung + gespeicherte Vorlieben. Die Seitengröße kommt immer vom Layout."""
    opts = ppd_options()
    rec = recommended_options(opts, paper, borderless)
    valid = {o["key"]: {c["value"] for c in o["choices"]} for o in opts}
    out = dict(rec)
    for k, v in (saved or {}).items():
        if k == "PageSize":
            continue
        if k in valid and v in valid[k]:
            out[k] = v
    return out


def apply_overrides(opts: dict, overrides: dict | None) -> dict:
    """Explizit im Druckdialog gewählte Optionen übernehmen (nur gültige Werte)."""
    valid = {o["key"]: {c["value"] for c in o["choices"]} for o in ppd_options()}
    out = dict(opts)
    for k, v in (overrides or {}).items():
        if k in valid and v in valid[k]:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Drucken & Jobs
# ---------------------------------------------------------------------------
def print_pdf(path: Path, options: dict, copies: int = 1, title: str = "MTG Proxies") -> str:
    if not queue_valid():
        setup_queue()
    if not queue_valid():
        raise RuntimeError(_state["last_error"] or "Druckerqueue nicht bereit")
    cmd = ["lp", "-d", config.PRINTER_QUEUE, "-n", str(max(1, min(int(copies), 99))), "-t", title[:120]]
    for k, v in options.items():
        cmd += ["-o", f"{k}={v}"]
    # 1:1 drucken – keine Skalierung, kein "An Seite anpassen"
    cmd += ["-o", "print-scaling=none", "-o", "fit-to-page=false", "-o", "number-up=1"]
    cmd.append(str(path))
    r = _run(cmd, timeout=120)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or "lp fehlgeschlagen")
    m = re.search(r"request id is (\S+)", r.stdout)
    return m.group(1) if m else r.stdout.strip()


_JOB_RE = re.compile(r"^(\S+-\d+)\s+(\S+)\s+(\d+)\s+(.+)$")


def jobs(limit: int = 15) -> list[dict]:
    if not cups_available():
        return []
    out = []
    for which in ("not-completed", "completed"):
        try:
            r = _run(["lpstat", "-W", which, "-o", config.PRINTER_QUEUE], timeout=5)
        except Exception:
            continue
        for line in r.stdout.splitlines():
            m = _JOB_RE.match(line.strip())
            if m:
                out.append({"id": m.group(1), "user": m.group(2), "size": int(m.group(3)),
                            "time": m.group(4).strip(), "active": which == "not-completed"})
    out.sort(key=lambda j: (not j["active"], -int(j["id"].rsplit("-", 1)[1])))
    out = out[:limit + sum(j["active"] for j in out)]
    for j in out:
        try:
            j.update(ipp.get_local_job(int(j["id"].rsplit("-", 1)[1])))
        except Exception as e:  # pragma: no cover - nur Diagnose
            j.update(state="aktiv" if j["active"] else "?", reasons=[], printer_message=str(e))
        bad = ("abort", "cancel", "error", "fail")
        j["ok"] = j.get("state_code") == 9 and not any(
            b in r for r in j.get("reasons", []) for b in bad)
    return out


def cups_log(lines: int = 200) -> str:
    path = Path("/var/log/cups/error_log")
    if not path.exists():
        return "(noch kein CUPS-Log vorhanden)"
    data = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(data[-lines:])


def cancel(job_id: str) -> None:
    if not re.fullmatch(r"[\w.\-]+-\d+", job_id):
        raise ValueError("ungültige Job-ID")
    r = _run(["cancel", job_id], timeout=10)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())


def cancel_all() -> None:
    _run(["cancel", "-a", config.PRINTER_QUEUE], timeout=10)
