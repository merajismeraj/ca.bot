import io
import zipfile

from app import archive


def mkzip(files, flag_utf8=True):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def entries(blob, name="dump.zip"):
    return list(archive.expand(name, io.BytesIO(blob)))


def test_nested_windows_and_junk():
    inner = mkzip({"Q2/sales sep 2026.csv": "x"})
    outer = mkzip({"Sharma\\Bank\\stmt aug 2026.csv": "a", "__MACOSX/._x": "j", "Thumbs.db": "j",
                   "~$draft.xlsx": "j", "inner.zip": inner, "deep/../evil.csv": "e"})
    got = {e.path: e.skipped for e in entries(outer)}
    assert "dump.zip/Sharma/Bank/stmt aug 2026.csv" in got
    assert "dump.zip/inner.zip/Q2/sales sep 2026.csv" in got
    assert "dump.zip/deep/evil.csv" in got  # no path traversal, ".." dropped
    assert not any("Thumbs" in p or "MACOSX" in p or "~$" in p for p in got)


def test_whatsapp_export_skips_chat_text():
    blob = mkzip({"WhatsApp Chat with Sharma Traders.txt": "chat", "IMG-20260914-WA0003.jpg": "img"})
    got = {e.path: e.skipped for e in entries(blob, "WhatsApp Chat - Sharma Traders.zip")}
    assert got["WhatsApp Chat - Sharma Traders.zip/IMG-20260914-WA0003.jpg"] is None
    assert "transcript" in got["WhatsApp Chat - Sharma Traders.zip/WhatsApp Chat with Sharma Traders.txt"]


def test_depth_limit_and_bad_zip():
    z = mkzip({"a.csv": "1"})
    for i in range(4):
        z = mkzip({f"level{i}.zip": z})
    skipped = [e for e in entries(z) if e.skipped]
    assert skipped and "nested too deep" in skipped[0].skipped
    assert "Not a valid zip" in entries(b"PK\x03\x04garbage")[0].skipped


def test_zip_bomb_guard():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("big.csv", b"0" * (20 * 1024 * 1024))
    e = entries(buf.getvalue())[0]
    assert e.skipped and "zip bomb" in e.skipped


def test_utf8_name_without_flag_is_repaired():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        info = zipfile.ZipInfo("शर्मा/bank.csv")
        z.writestr(info, "x")
    raw = bytearray(buf.getvalue())
    # clear the UTF-8 flag (bit 11) in local + central headers, as 7-Zip/macOS often do
    for sig in (b"PK\x03\x04", b"PK\x01\x02"):
        i = raw.find(sig)
        off = i + (6 if sig == b"PK\x03\x04" else 8)
        flags = int.from_bytes(raw[off:off + 2], "little") & ~0x800
        raw[off:off + 2] = flags.to_bytes(2, "little")
    assert entries(bytes(raw))[0].path == "dump.zip/शर्मा/bank.csv"


def test_upload_reports_skips_and_export_roundtrip(api):
    c = api.post("/api/clients", json={"name": "Sharma Traders", "gstin": "27ABCPS1234K1Z5"}).json()
    blob = mkzip({"Sharma Traders/GST/Sales register Sep-2026.csv": "Sales 27ABCPS1234K1Z5",
                  "Sharma Traders/GST/Sales register Aug-2026.csv": "Sales 27ABCPS1234K1Z5 aug",
                  "Sharma Traders/notes.exe": "MZ"})
    r = api.post("/api/upload", files=[("files", ("office.zip", blob, "application/zip"))]).json()
    assert r["auto"] == 2 and any("Unsupported" in s["reason"] for s in r["skipped"])
    exp = api.get(f"/api/clients/{c['id']}/export")
    assert exp.status_code == 200 and exp.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(exp.content)).namelist()
    assert "Sharma Traders/FY 2026-27/Sales register - invoices/2026-09 Sales register Sep-2026.csv" in names
    assert {"Sharma Traders/_index.csv", "Sharma Traders/_filings.csv", "Sharma Traders/_profile.json"} <= set(names)
    assert api.get("/api/export").status_code == 200
    api.patch(f"/api/users/{api.staff_id}", json={"perms": []})
    assert api.get(f"/api/clients/{c['id']}/export", headers=api.staff).status_code == 403
