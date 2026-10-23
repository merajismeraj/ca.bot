import io
import shutil

import pytest

pytestmark = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")


def photo(text_lines):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1200, 120 + 60 * len(text_lines)), "white")
    d = ImageDraw.Draw(img)
    for i, t in enumerate(text_lines):
        d.text((50, 50 + 60 * i), t, fill="black", font=ImageFont.load_default(size=34))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_photo_is_read_and_filed(api):
    from app import ocr
    c = api.post("/api/clients", json={"name": "Sharma Traders", "gstin": "27ABCPS1234K1Z5"}).json()
    img = photo(["PURCHASE REGISTER - SEPTEMBER 2026", "GSTIN: 27ABCPS1234K1Z5", "Bill PB/101  12,500"])
    r = api.post("/api/upload", files=[("files", ("IMG-20261003-WA0011.jpg", img, "image/jpeg"))]).json()
    assert r["reading"] == 1 and r["files"][0]["ocr_status"] == "pending"
    assert ocr.process_pending() == 1
    d = api.get(f"/api/documents?client_id={c['id']}").json()[0]
    assert (d["doc_type"], d["period_start"], d["status"], d["ocr_status"]) == ("purchase_register", "2026-09", "auto", "done")


def test_unreadable_scan_stays_in_review(api):
    from app import ocr
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (300, 300), (200, 30, 30)).save(buf, format="PNG")
    api.post("/api/upload", files=[("files", ("scan.png", buf.getvalue(), "image/png"))])
    ocr.process_pending()
    d = api.get("/api/documents?status=needs_review").json()[0]
    assert d["ocr_status"] == "failed" and "Couldn't read" in d["reasons"][0]


def test_confirmed_document_is_never_overwritten(api):
    from app import db, engine
    c = api.post("/api/clients", json={"name": "Sharma Traders", "gstin": "27ABCPS1234K1Z5"}).json()
    r = api.post("/api/upload", files=[("files", ("x.png", photo(["hello"]), "image/png"))]).json()
    did = r["files"][0]["id"]
    api.post(f"/api/documents/{did}/confirm", json={"client_id": c["id"], "doc_type": "gstr2b", "period_start": "2026-08"})
    with db.tx() as conn:
        engine.reclassify(conn, did, "SALES REGISTER SEPTEMBER 2026 GSTIN 27ABCPS1234K1Z5", "ocr")
    d = api.get(f"/api/documents?client_id={c['id']}").json()[0]
    assert (d["doc_type"], d["period_start"]) == ("gstr2b", "2026-08")
