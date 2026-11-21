import io

from fastapi.testclient import TestClient
from PIL import Image

from mtgprint.main import app, parse_pages


def _png(w=822, h=1122, color=(200, 0, 0)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def test_parse_pages():
    assert parse_pages("", 3) == [0, 1, 2]
    assert parse_pages("1-2, 3", 5) == [0, 1, 2]
    assert parse_pages("2-", 4) == [1, 2, 3]


def test_upload_place_pdf_flow():
    with TestClient(app) as c:
        pid = c.get("/api/projects").json()[0]["id"]
        files = [("files", (f"Karte {i}.png", _png(color=(i * 20, 0, 0)), "image/png")) for i in range(10)]
        r = c.post("/api/cards/upload", data={"bleed_mode": "mpc", "project_id": pid, "quantity": "1"}, files=files)
        body = r.json()
        assert len(body["added"]) == 10 and body["placed"] == 10
        # Duplikat wird erkannt
        r = c.post("/api/cards/upload", data={"bleed_mode": "mpc"}, files=[files[0]])
        assert r.json()["existing"] and not r.json()["added"]
        p = c.get(f"/api/projects/{pid}").json()
        assert len(p["pages"]) == 2 and p["geometry"]["num_cuts"] == 8
        # Ohne-Padding-Bild
        r = c.post("/api/cards/upload", data={"bleed_mode": "none"}, files=[("files", ("scry.png", _png(745, 1040), "image/png"))])
        cid = r.json()["added"][0]
        card = next(x for x in c.get("/api/cards").json() if x["id"] == cid)
        assert card["suggested_mode"] == "none" and card["synthetic_bleed"]
        # Layout ändern -> Seiten werden umgebrochen
        p = c.put(f"/api/projects/{pid}", json={"layout": {"cols": 2, "rows": 2}}).json()
        assert len(p["pages"]) == 3 and len(p["pages"][0]) == 4
        r = c.post(f"/api/projects/{pid}/pdf", json={"pages": "1-2"})
        assert r.status_code == 200 and r.json()["pages"] == 2
        pdf = c.get(r.json()["url"])
        assert pdf.content.startswith(b"%PDF")
        # Kalibrierung
        r = c.post("/api/calibration/compute", json={"a": 20.5, "b": 190.3, "c": 19.6, "d": 276.8})
        res = r.json()["result"]
        assert abs(res["offset_x"] - (20.5 - res["scale_x"] * 20)) < 1e-3
        r = c.post("/api/calibration/page", json={"action": "pdf"})
        assert r.status_code == 200
        # Drucken ohne konfigurierten Drucker -> sauberer Fehler, kein Crash
        r = c.post(f"/api/projects/{pid}/print", json={})
        assert r.status_code == 503 and "Drucker" in r.json()["detail"]
