"""FastAPI-Anwendung: REST-API + Web-Oberfläche."""
from __future__ import annotations

import hashlib
import io
import logging
import re
import shutil
import tempfile
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, calibration, config, imaging, mpcfill, pdfgen, printer, storage
from .layout import geometry, normalize_layout, reflow, slots_per_page

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mtgprint")

STATIC_DIR = Path(__file__).parent / "static"
MAX_ZIP_BYTES = 4 * 1024 ** 3


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    if printer.configured():
        threading.Thread(target=printer.setup_loop, daemon=True, name="printer-setup").start()
    yield


app = FastAPI(title="MTG Print Studio", version=__version__, lifespan=lifespan)


@app.exception_handler(KeyError)
async def _key_error(_, exc):
    return JSONResponse({"detail": f"Nicht gefunden: {exc}"}, status_code=404)


@app.exception_handler(ValueError)
async def _value_error(_, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(RuntimeError)
async def _runtime_error(_, exc):
    # z.B. Drucker nicht erreichbar / CUPS-Fehler -> verständliche Meldung im UI
    return JSONResponse({"detail": str(exc)}, status_code=503)


# ---------------------------------------------------------------------------
# Allgemein
# ---------------------------------------------------------------------------
@app.get("/api/info")
def info():
    return {
        "version": __version__,
        "printer_configured": printer.configured(),
        "printer_uri": config.printer_uri(),
        "bleed_modes": imaging.BLEED_MODES,
        "papers": list(config.PAPER_SIZES_MM),
        "card_default": [config.DEFAULT_CARD_W_MM, config.DEFAULT_CARD_H_MM],
        "calibration_ref": calibration.REF,
    }


@app.get("/api/settings")
def get_settings():
    return storage.get_settings()


@app.patch("/api/settings")
def patch_settings(patch: dict = Body(...)):
    return storage.update_settings(patch)


@app.post("/api/geometry")
def post_geometry(layout: dict = Body(...)):
    return geometry(normalize_layout(layout))


# ---------------------------------------------------------------------------
# Karten
# ---------------------------------------------------------------------------
def _card_public(c: dict) -> dict:
    out = dict(c)
    out["thumb"] = f"/api/cards/{c['id']}/thumb?v={c.get('thumb_v', 0)}"
    return out


def _refresh_card(card: dict) -> dict:
    path = config.CARDS_DIR / card["file"]
    w, h = imaging.image_size(path)
    card["w"], card["h"] = w, h
    card["suggested_mode"] = imaging.suggest_mode(w, h)
    card["dpi"] = round(imaging.effective_dpi(w, h, card))
    thumb = imaging.make_thumbnail(path, card, config.THUMBS_DIR / f"{card['id']}.jpg")
    card.update(fx=thumb["fx"], fy=thumb["fy"], synthetic_bleed=thumb["synthetic"])
    card["thumb_v"] = int(card.get("thumb_v", 0)) + 1
    return card


def _ingest(name: str, data: bytes, opts: dict, cards: dict) -> tuple[dict, bool]:
    digest = hashlib.sha1(data).hexdigest()[:16]
    if digest in cards:
        return cards[digest], False
    ext = Path(name).suffix.lower()
    if ext == ".jpeg":
        ext = ".jpg"
    fname = f"{digest}{ext}"
    (config.CARDS_DIR / fname).write_bytes(data)
    w, h = imaging.image_size(config.CARDS_DIR / fname)
    card = {
        "id": digest,
        "name": storage.clean_name(Path(name).name),
        "original_name": Path(name).name,
        "file": fname,
        "bleed_mode": opts["bleed_mode"],
        "bleed_mm": opts["bleed_mm"],
        "fill_corners": opts["fill_corners"],
        "bleed_fill": opts.get("bleed_fill", "edge"),
        "rotation": 90 if (opts["auto_rotate"] and w > h) else 0,
        "created": time.time(),
    }
    m = re.search(r"([A-Za-z0-9_\-]{25,})", Path(name).stem)
    if m:
        card["drive_id"] = m.group(1)
    _refresh_card(card)
    cards[digest] = card
    return card, True


def _place(project: dict, ids: list[str]) -> int:
    """Hängt Karten hinter dem letzten belegten Slot an (absichtliche Lücken bleiben)."""
    per = slots_per_page(project["layout"])
    flat = [s for p in project["pages"] for s in p]
    while flat and flat[-1] is None:
        flat.pop()
    flat.extend(ids)
    project["pages"] = reflow([flat], per)
    return len(ids)


@app.get("/api/cards")
def list_cards():
    cards = storage.get_cards()
    return sorted((_card_public(c) for c in cards.values()), key=lambda c: c.get("created", 0))


@app.post("/api/cards/upload")
def upload_cards(
    files: list[UploadFile] = File(...),
    bleed_mode: str = Form("mpc"),
    bleed_mm: float = Form(3.0),
    fill_corners: bool = Form(True),
    auto_rotate: bool = Form(True),
    project_id: str = Form(""),
    quantity: int = Form(0),
):
    if bleed_mode not in imaging.BLEED_MODES:
        raise ValueError("Unbekannter Bleed-Modus")
    opts = {"bleed_mode": bleed_mode, "bleed_mm": bleed_mm, "fill_corners": fill_corners,
            "auto_rotate": auto_rotate}
    cards = storage.get_cards()
    added, existing, errors, order = [], [], [], []

    def handle(name: str, data: bytes):
        if Path(name).suffix.lower() not in config.IMAGE_EXTENSIONS:
            return
        try:
            card, new = _ingest(name, data, opts, cards)
            (added if new else existing).append(card["id"])
            order.append(card["id"])
        except Exception as e:
            log.exception("Upload fehlgeschlagen: %s", name)
            errors.append(f"{Path(name).name}: {e}")

    for f in files:
        name = f.filename or "upload"
        if name.lower().endswith(".zip"):
            with tempfile.TemporaryFile() as tmp:
                shutil.copyfileobj(f.file, tmp)
                tmp.seek(0)
                try:
                    with zipfile.ZipFile(tmp) as z:
                        total = 0
                        members = sorted((i for i in z.infolist() if not i.is_dir()), key=lambda i: i.filename)
                        for info_ in members:
                            if "__MACOSX" in info_.filename or Path(info_.filename).name.startswith("."):
                                continue
                            total += info_.file_size
                            if total > MAX_ZIP_BYTES:
                                errors.append("ZIP zu groß – Rest übersprungen")
                                break
                            handle(info_.filename, z.read(info_))
                except zipfile.BadZipFile:
                    errors.append(f"{name}: keine gültige ZIP-Datei")
        else:
            handle(name, f.file.read())
    storage.save_cards(cards)

    placed = 0
    if project_id and quantity > 0 and order:
        project = storage.get_project(project_id)
        ids = []
        for cid in dict.fromkeys(order):  # Reihenfolge des Uploads, ohne Duplikate
            ids.extend([cid] * quantity)
        placed = _place(project, ids)
        storage.save_project(project)
    return {"added": added, "existing": existing, "errors": errors, "placed": placed}


@app.patch("/api/cards/{card_id}")
def patch_card(card_id: str, patch: dict = Body(...)):
    return _card_public(_update_cards([card_id], patch)[0])


@app.post("/api/cards/bulk")
def bulk_cards(payload: dict = Body(...)):
    return [_card_public(c) for c in _update_cards(payload.get("ids", []), payload.get("patch", {}))]


def _update_cards(ids: list[str], patch: dict) -> list[dict]:
    cards = storage.get_cards()
    allowed = {"name", "bleed_mode", "bleed_mm", "fill_corners", "rotation", "bleed_fill"}
    out = []
    for cid in ids:
        card = cards[cid]
        relevant = False
        for k, v in patch.items():
            if k not in allowed:
                continue
            if k == "bleed_mode" and v not in imaging.BLEED_MODES:
                raise ValueError("Unbekannter Bleed-Modus")
            if k == "rotation":
                v = int(v) % 360
                if v not in (0, 90, 180, 270):
                    raise ValueError("Rotation muss 0/90/180/270 sein")
            if k == "bleed_mm":
                v = max(0.0, min(float(v), 10.0))
            if card.get(k) != v:
                card[k] = v
                relevant = relevant or k != "name"
        if relevant:
            _refresh_card(card)
        out.append(card)
    storage.save_cards(cards)
    return out


@app.post("/api/cards/delete")
def delete_cards(payload: dict = Body(...)):
    cards = storage.get_cards()
    removed_slots = 0
    for cid in payload.get("ids", []):
        card = cards.pop(cid, None)
        if not card:
            continue
        for p in (config.CARDS_DIR / card["file"], config.THUMBS_DIR / f"{cid}.jpg"):
            p.unlink(missing_ok=True)
        removed_slots += storage.remove_card_from_projects(cid)
    storage.save_cards(cards)
    return {"ok": True, "removed_slots": removed_slots}


@app.get("/api/cards/{card_id}/thumb")
def card_thumb(card_id: str):
    path = config.THUMBS_DIR / f"{card_id}.jpg"
    if not path.exists():
        cards = storage.get_cards()
        if card_id not in cards:
            raise HTTPException(404)
        _refresh_card(cards[card_id])
        storage.save_cards(cards)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=31536000"})


@app.get("/api/cards/{card_id}/original")
def card_original(card_id: str):
    card = storage.get_cards()[card_id]
    return FileResponse(config.CARDS_DIR / card["file"], filename=card["original_name"])


# ---------------------------------------------------------------------------
# Projekte
# ---------------------------------------------------------------------------
def _project_out(p: dict) -> dict:
    out = dict(p)
    out["geometry"] = geometry(p["layout"])
    return out


@app.get("/api/projects")
def list_projects():
    projects = storage.list_projects()
    if not projects:
        storage.create_project("Mein erstes Projekt")
        projects = storage.list_projects()
    return projects


@app.post("/api/projects")
def create_project(payload: dict = Body(default={})):
    src = payload.get("copy_from")
    if src:
        base = storage.get_project(src)
        p = storage.create_project(payload.get("name") or f"{base['name']} (Kopie)", base["layout"])
        p["pages"] = [list(pg) for pg in base["pages"]]
        p = storage.save_project(p)
    else:
        p = storage.create_project(payload.get("name") or "Neues Projekt")
    return _project_out(p)


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    p = storage.get_project(pid)
    storage.update_settings({"last_project": pid})
    return _project_out(p)


@app.put("/api/projects/{pid}")
def put_project(pid: str, payload: dict = Body(...)):
    p = storage.get_project(pid)
    if "name" in payload:
        p["name"] = str(payload["name"])[:200] or p["name"]
    if "layout" in payload:
        old_per = slots_per_page(p["layout"])
        p["layout"] = normalize_layout({**p["layout"], **payload["layout"]})
        new_per = slots_per_page(p["layout"])
        if old_per != new_per and "pages" not in payload:
            p["pages"] = reflow(p["pages"], new_per)
    if "pages" in payload:
        cards = storage.get_cards()
        pages = []
        for page in payload["pages"]:
            pages.append([s if (s and s in cards) else None for s in page])
        p["pages"] = pages
    return _project_out(storage.save_project(p))


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    storage.delete_project(pid)
    return {"ok": True}


def parse_pages(spec: str | None, n: int) -> list[int]:
    """'1-3,5' -> [0,1,2,4]; leer -> alle Seiten."""
    if not spec or not str(spec).strip():
        return list(range(n))
    out = []
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a = int(a) if a else 1
            b = int(b) if b else n
            out.extend(range(a - 1, b))
        else:
            out.append(int(part) - 1)
    res = [i for i in dict.fromkeys(out) if 0 <= i < n]
    if not res:
        raise ValueError(f"Keine gültigen Seiten in '{spec}' (Projekt hat {n} Seiten)")
    return res


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_")[:50] or "proxies"


def _cleanup_exports(keep: int = 40):
    files = sorted(config.EXPORTS_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files[keep:]:
        f.unlink(missing_ok=True)


def _render_project(pid: str, payload: dict) -> tuple[Path, dict, dict]:
    p = storage.get_project(pid)
    pages = parse_pages(payload.get("pages"), len(p["pages"]))
    profile = None
    if payload.get("calibrated", True):
        profile = storage.get_profile(payload.get("profile_id"))
    cards = storage.get_cards()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = "" if profile and profile["id"] != "default" else "_unkalibriert"
    out = config.EXPORTS_DIR / f"{_slug(p['name'])}_{stamp}{suffix}.pdf"
    result = pdfgen.build_project_pdf(p, cards, out, profile, pages)
    _cleanup_exports()
    return out, result, p


@app.post("/api/projects/{pid}/pdf")
def project_pdf(pid: str, payload: dict = Body(default={})):
    out, result, _ = _render_project(pid, payload)
    return {**result, "url": f"/api/exports/{out.name}", "file": out.name}


@app.post("/api/projects/{pid}/print")
def project_print(pid: str, payload: dict = Body(default={})):
    out, result, p = _render_project(pid, payload)
    if result["cards"] == 0:
        raise ValueError("Die ausgewählten Seiten enthalten keine Karten")
    s = storage.get_settings()
    opts = printer.effective_options({**s["print"]["options"], **(payload.get("options") or {})})
    job = printer.print_pdf(out, opts, payload.get("copies", 1), title=p["name"])
    return {**result, "job": job, "options": opts, "url": f"/api/exports/{out.name}"}


@app.post("/api/projects/{pid}/import-mpcfill")
def import_mpcfill(pid: str, file: UploadFile = File(...), download_missing: bool = Form(False),
                   include_backs: bool = Form(False), replace: bool = Form(True),
                   bleed_mode: str = Form("mpc")):
    project = storage.get_project(pid)
    order = mpcfill.parse_order(file.file.read())
    cards = storage.get_cards()
    entries = list(order["fronts"])
    if include_backs:
        entries += [b for b in order["backs"] if b["id"] != order["cardback"]]
    mapping = mpcfill.match_cards(entries, cards)
    downloaded, missing = [], []
    if download_missing:
        opts = {"bleed_mode": bleed_mode, "bleed_mm": 3.0, "fill_corners": True, "auto_rotate": True}
        for e in entries:
            if mapping.get(e["id"]):
                continue
            try:
                with tempfile.TemporaryDirectory() as td:
                    path = mpcfill.download_drive_image(e["id"], Path(td))
                    name = f"{e['name'] or e['id']} ({e['id']}){path.suffix}"
                    card, _ = _ingest(name, path.read_bytes(), opts, cards)
                    card["drive_id"] = e["id"]
                    mapping[e["id"]] = card["id"]
                    downloaded.append(e["name"] or e["id"])
            except Exception as ex:
                log.warning("Download %s fehlgeschlagen: %s", e["id"], ex)
        storage.save_cards(cards)
    for e in entries:
        if not mapping.get(e["id"]):
            missing.append(e["name"] or e["id"])

    # Reihenfolge nach MPC-Slots (Vorderseiten), danach die Rückseiten
    seq = []
    for section in (order["fronts"], [b for b in entries if b in order["backs"]]):
        slot_map = {}
        for e in section:
            for s in e["slots"] or [len(slot_map)]:
                slot_map[s] = mapping.get(e["id"])
        seq.extend(slot_map[k] for k in sorted(slot_map))
    ids = [c for c in seq if c]
    if replace:
        project["pages"] = [[None] * slots_per_page(project["layout"])]
    _place(project, ids)
    project = storage.save_project(project)
    return {"placed": len(ids), "missing": sorted(set(missing)), "downloaded": downloaded,
            "project": _project_out(project)}


@app.get("/api/exports/{name}")
def get_export(name: str, download: bool = False):
    if not re.fullmatch(r"[\w\-.]+\.pdf", name):
        raise HTTPException(404)
    path = config.EXPORTS_DIR / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/pdf",
                        filename=name if download else None,
                        content_disposition_type="attachment" if download else "inline")


# ---------------------------------------------------------------------------
# Drucker
# ---------------------------------------------------------------------------
@app.get("/api/printer")
def printer_status():
    st = printer.status_basic()
    attrs, err = printer.printer_attributes() if st["configured"] else (None, "")
    s = storage.get_settings()
    opts = printer.ppd_options() if st["queue_ready"] else []
    return {
        **st,
        "attributes": attrs,
        "attributes_error": err,
        "queue_state": printer.queue_state() if st["queue_ready"] else "",
        "options": opts,
        "recommended": printer.recommended_options(opts) if opts else {},
        "effective": printer.effective_options(s["print"]["options"]) if opts else {},
        "saved": s["print"]["options"],
    }


@app.post("/api/printer/setup")
def printer_setup():
    return printer.setup_queue(force=True)


@app.post("/api/printer/options")
def printer_options(payload: dict = Body(...)):
    s = storage.get_settings()
    s["print"]["options"] = {k: str(v) for k, v in (payload.get("options") or {}).items()}
    storage.save_settings(s)
    return {"effective": printer.effective_options(s["print"]["options"])}


@app.get("/api/printer/jobs")
def printer_jobs():
    return printer.jobs()


@app.post("/api/printer/jobs/{job_id}/cancel")
def printer_cancel(job_id: str):
    printer.cancel(job_id)
    return {"ok": True}


@app.post("/api/printer/jobs/cancel-all")
def printer_cancel_all():
    printer.cancel_all()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Kalibrierung
# ---------------------------------------------------------------------------
@app.get("/api/calibration")
def calibration_info():
    s = storage.get_settings()
    return {
        "profiles": [{**p, "description": calibration.describe(p)} for p in s["profiles"]],
        "active": s["active_profile"],
        "ref": calibration.REF,
    }


@app.post("/api/calibration/profiles")
def calibration_new(payload: dict = Body(default={})):
    s = storage.get_settings()
    base = None
    if payload.get("copy_from"):
        base = next((p for p in s["profiles"] if p["id"] == payload["copy_from"]), None)
    p = calibration.new_profile(payload.get("name", ""), base)
    s["profiles"].append(p)
    if payload.get("activate", True):
        s["active_profile"] = p["id"]
    storage.save_settings(s)
    return p


@app.put("/api/calibration/profiles/{prof_id}")
def calibration_update(prof_id: str, payload: dict = Body(...)):
    s = storage.get_settings()
    for p in s["profiles"]:
        if p["id"] == prof_id:
            for k in ("name", "notes"):
                if k in payload:
                    p[k] = str(payload[k])
            for k in ("offset_x", "offset_y"):
                if k in payload:
                    p[k] = round(max(-30.0, min(30.0, float(payload[k]))), 3)
            for k in ("scale_x", "scale_y"):
                if k in payload:
                    p[k] = round(max(0.9, min(1.1, float(payload[k]))), 6)
            if "measurements" in payload:
                p["measurements"] = payload["measurements"]
            storage.save_settings(s)
            return {**p, "description": calibration.describe(p)}
    raise KeyError(prof_id)


@app.delete("/api/calibration/profiles/{prof_id}")
def calibration_delete(prof_id: str):
    if prof_id == "default":
        raise ValueError("Das Standardprofil kann nicht gelöscht werden")
    s = storage.get_settings()
    s["profiles"] = [p for p in s["profiles"] if p["id"] != prof_id]
    if s["active_profile"] == prof_id:
        s["active_profile"] = "default"
    storage.save_settings(s)
    return {"ok": True}


@app.post("/api/calibration/active")
def calibration_active(payload: dict = Body(...)):
    s = storage.get_settings()
    if not any(p["id"] == payload.get("id") for p in s["profiles"]):
        raise KeyError(payload.get("id"))
    s["active_profile"] = payload["id"]
    storage.save_settings(s)
    return {"active": s["active_profile"]}


@app.post("/api/calibration/compute")
def calibration_compute(payload: dict = Body(...)):
    m = calibration.from_measurements(float(payload["a"]), float(payload["b"]),
                                      float(payload["c"]), float(payload["d"]))
    base = calibration.DEFAULT_PROFILE
    if payload.get("printed_with_profile", True):
        base = storage.get_profile(payload.get("profile_id"))
    total = calibration.combine(base, m)
    return {"measured": m, "result": total, "description": calibration.describe(total)}


def _deliver(path: Path, action: str, title: str):
    if action == "print":
        s = storage.get_settings()
        opts = printer.effective_options(s["print"]["options"])
        job = printer.print_pdf(path, opts, 1, title=title)
        return {"job": job, "url": f"/api/exports/{path.name}", "options": opts}
    return {"url": f"/api/exports/{path.name}"}


@app.post("/api/calibration/page")
def calibration_page(payload: dict = Body(default={})):
    profile = None if payload.get("uncalibrated") else storage.get_profile(payload.get("profile_id"))
    out = config.EXPORTS_DIR / f"Kalibrierseite_{time.strftime('%Y%m%d-%H%M%S')}.pdf"
    pdfgen.build_calibration_pdf(out, profile)
    return _deliver(out, payload.get("action", "pdf"), "Kalibrierseite")


@app.post("/api/calibration/cut-test")
def cut_test(payload: dict = Body(default={})):
    profile = None if payload.get("uncalibrated") else storage.get_profile(payload.get("profile_id"))
    layout = storage.get_settings()["default_layout"]
    if payload.get("project_id"):
        layout = storage.get_project(payload["project_id"])["layout"]
    out = config.EXPORTS_DIR / f"Probeschnitt_{time.strftime('%Y%m%d-%H%M%S')}.pdf"
    pdfgen.build_cut_test_pdf(out, layout, profile)
    return _deliver(out, payload.get("action", "pdf"), "Probeschnitt")


# ---------------------------------------------------------------------------
# Statische Oberfläche
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
