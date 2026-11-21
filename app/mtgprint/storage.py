"""Persistenz als JSON-Dateien im Datenverzeichnis (einfach zu sichern / einzusehen)."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path

from . import calibration, config
from .layout import DEFAULT_LAYOUT, normalize_layout, reflow, slots_per_page

_lock = threading.RLock()

DEFAULT_SETTINGS = {
    "default_layout": DEFAULT_LAYOUT,
    "upload_defaults": {
        "bleed_mode": "mpc",
        "bleed_mm": 3.0,
        "fill_corners": True,
        "auto_rotate": True,
        "bleed_fill": "edge",
    },
    "print": {
        "options": {},          # CUPS/PPD-Optionen, z.B. {"MediaType": "PhotographicGlossy"}
        "use_calibration": True,
    },
    "profiles": [calibration.DEFAULT_PROFILE],
    "active_profile": "default",
    "last_project": None,
}


def _read(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return deepcopy(default)
    except json.JSONDecodeError:
        backup = path.with_suffix(path.suffix + f".broken-{int(time.time())}")
        path.rename(backup)
        return deepcopy(default)


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------
def get_settings() -> dict:
    with _lock:
        s = _read(config.SETTINGS_FILE, DEFAULT_SETTINGS)
        for k, v in DEFAULT_SETTINGS.items():
            if k not in s:
                s[k] = deepcopy(v)
            elif isinstance(v, dict):
                for k2, v2 in v.items():
                    s[k].setdefault(k2, deepcopy(v2))
        s["default_layout"] = normalize_layout(s["default_layout"])
        if not any(p["id"] == "default" for p in s["profiles"]):
            s["profiles"].insert(0, deepcopy(calibration.DEFAULT_PROFILE))
        return s


def save_settings(s: dict) -> dict:
    with _lock:
        _write(config.SETTINGS_FILE, s)
        return s


def update_settings(patch: dict) -> dict:
    with _lock:
        s = get_settings()
        for k, v in patch.items():
            if k not in DEFAULT_SETTINGS:
                continue
            if k == "default_layout":
                s[k] = normalize_layout({**s[k], **v})
            elif isinstance(DEFAULT_SETTINGS[k], dict) and isinstance(v, dict):
                s[k].update(v)
            else:
                s[k] = v
        return save_settings(s)


def get_profile(profile_id: str | None) -> dict | None:
    s = get_settings()
    pid = profile_id or s["active_profile"]
    for p in s["profiles"]:
        if p["id"] == pid:
            return p
    return s["profiles"][0]


# ---------------------------------------------------------------------------
# Karten
# ---------------------------------------------------------------------------
def get_cards() -> dict:
    with _lock:
        return _read(config.CARDS_FILE, {})


def save_cards(cards: dict) -> None:
    with _lock:
        _write(config.CARDS_FILE, cards)


def clean_name(filename: str) -> str:
    name = Path(filename).stem
    # MPC-Fill hängt oft die Google-Drive-ID in Klammern an: "Sol Ring (1AbC...xyz)"
    name = re.sub(r"\s*[\(\[][A-Za-z0-9_\-]{20,}[\)\]]\s*$", "", name)
    name = name.replace("_", " ").strip()
    return name or Path(filename).stem


# ---------------------------------------------------------------------------
# Projekte
# ---------------------------------------------------------------------------
def _project_path(pid: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{8,32}", pid or ""):
        raise KeyError(pid)
    return config.PROJECTS_DIR / f"{pid}.json"


def list_projects() -> list[dict]:
    out = []
    for p in sorted(config.PROJECTS_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        n = sum(1 for page in data.get("pages", []) for s in page if s)
        out.append({"id": data["id"], "name": data.get("name", "?"), "updated": data.get("updated", 0),
                    "pages": len(data.get("pages", [])), "cards": n})
    out.sort(key=lambda x: -x["updated"])
    return out


def get_project(pid: str) -> dict:
    with _lock:
        path = _project_path(pid)
        if not path.exists():
            raise KeyError(pid)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["layout"] = normalize_layout(data.get("layout"))
        per = slots_per_page(data["layout"])
        pages = data.get("pages") or [[None] * per]
        if any(len(p) != per for p in pages):
            pages = reflow(pages, per)
        data["pages"] = pages
        return data


def save_project(data: dict) -> dict:
    with _lock:
        data["updated"] = time.time()
        data["layout"] = normalize_layout(data.get("layout"))
        per = slots_per_page(data["layout"])
        pages = data.get("pages") or [[None] * per]
        if any(len(p) != per for p in pages):
            pages = reflow(pages, per)
        data["pages"] = pages
        _write(_project_path(data["id"]), data)
        return data


def create_project(name: str, layout: dict | None = None) -> dict:
    s = get_settings()
    lay = normalize_layout(layout or s["default_layout"])
    data = {
        "id": uuid.uuid4().hex[:12],
        "name": name or "Neues Projekt",
        "created": time.time(),
        "layout": lay,
        "pages": [[None] * slots_per_page(lay)],
    }
    return save_project(data)


def delete_project(pid: str) -> None:
    with _lock:
        path = _project_path(pid)
        if path.exists():
            path.unlink()


def remove_card_from_projects(card_id: str) -> int:
    n = 0
    with _lock:
        for meta in list_projects():
            p = get_project(meta["id"])
            changed = False
            for page in p["pages"]:
                for i, s in enumerate(page):
                    if s == card_id:
                        page[i] = None
                        changed = True
                        n += 1
            if changed:
                save_project(p)
    return n
