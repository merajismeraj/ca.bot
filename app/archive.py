"""Zip in, zip out.

In: offices send whatever they have - a zip of folders, zips inside zips,
Windows-made zips with backslashes and non-UTF-8 names, WhatsApp "Export chat"
zips. `expand` streams entries one at a time from the uploaded file (never the
whole archive in memory) and reports anything it had to skip, with a reason a
person can act on.

Out: `export_clients` writes a client's vault as an organised zip
(Client / FY / Document type / YYYY-MM file) with an index and the filing
register, so the office can hand a complete file to an auditor or bank.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Iterator

from . import db, storage
from . import fy as F
from .catalog import DOC_TYPES

MAX_DEPTH = 3                                   # zip inside zip inside zip
MAX_ENTRIES = int(os.environ.get("CABOT_ZIP_MAX_FILES", 10000))
MAX_FILE = 100 * 1024 * 1024                    # one document
MAX_TOTAL = int(os.environ.get("CABOT_ZIP_MAX_MB", 4096)) * 1024 * 1024
MAX_RATIO = 200                                 # compressed:uncompressed beyond this smells like a zip bomb

JUNK = re.compile(r"(^|/)(__MACOSX|\.DS_Store|Thumbs\.db|desktop\.ini|\.git|~\$[^/]*|\._[^/]*)(/|$)", re.I)
WA_CHAT = re.compile(r"(^|/)(_chat\.txt|WhatsApp Chat[^/]*\.txt)$", re.I)


@dataclass
class Entry:
    path: str
    data: bytes | None = None
    skipped: str | None = None


def is_zip(name: str, head: bytes = b"") -> bool:
    return name.lower().endswith(".zip") or head[:4] == b"PK\x03\x04"


def clean_name(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if not info.flag_bits & 0x800:
        # no UTF-8 flag: Python decoded as cp437. 7-Zip/macOS often wrote UTF-8 anyway - undo the mis-decoding.
        try:
            name = name.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    name = name.replace("\\", "/")
    parts = [p for p in PurePosixPath(name).parts if p not in ("", ".", "..", "/")]
    return "/".join(parts)


def expand(name: str, fileobj: IO[bytes], _depth: int = 0, _budget: list | None = None) -> Iterator[Entry]:
    """Yield every document inside an uploaded zip (recursing into nested zips)."""
    budget = _budget if _budget is not None else [MAX_TOTAL, MAX_ENTRIES]
    root = Path(name).name
    try:
        z = zipfile.ZipFile(fileobj)
    except zipfile.BadZipFile:
        yield Entry(root, skipped="Not a valid zip file (is it a partial download?)")
        return
    with z:
        for info in z.infolist():
            if info.is_dir():
                continue
            rel = clean_name(info)
            path = f"{root}/{rel}"
            if not rel or JUNK.search(rel):
                continue
            if WA_CHAT.search(rel):
                yield Entry(path, skipped="WhatsApp chat transcript — attachments were kept, the chat text wasn't")
                continue
            if info.flag_bits & 0x1:
                yield Entry(path, skipped="Password-protected inside the zip — unzip it with the password and upload again")
                continue
            if budget[1] <= 0:
                yield Entry(path, skipped=f"Zip has more than {MAX_ENTRIES} files — split it")
                return
            if info.file_size > MAX_FILE:
                yield Entry(path, skipped="Larger than 100 MB")
                continue
            if info.compress_size and info.file_size / info.compress_size > MAX_RATIO and info.file_size > 10 * 1024 * 1024:
                yield Entry(path, skipped="Compression ratio looks unsafe (possible zip bomb)")
                continue
            if info.file_size > budget[0]:
                yield Entry(path, skipped="Upload exceeds the total size limit")
                return
            try:
                data = z.read(info)
            except (RuntimeError, zipfile.BadZipFile, NotImplementedError, OSError) as e:
                yield Entry(path, skipped=f"Couldn't read this entry ({type(e).__name__})")
                continue
            budget[0] -= len(data)
            budget[1] -= 1
            if is_zip(rel, data[:4]):
                if _depth + 1 >= MAX_DEPTH:
                    yield Entry(path, skipped="Zip nested too deep — unzip one level and upload again")
                    continue
                for e in expand(rel, io.BytesIO(data), _depth + 1, budget):
                    e.path = f"{root}/{e.path}"
                    yield e
                continue
            yield Entry(path, data)


# ----------------------------------------------------------------- export
def _safe(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", s).strip(" .")
    return s[:120] or "untitled"


def export_clients(conn, client_ids: list[int], out: IO[bytes]) -> int:
    """Write an organised zip of the given clients' documents. Returns the number of files written."""
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for cid in client_ids:
            c = db.row(conn, "SELECT * FROM clients WHERE id=?", (cid,))
            if not c:
                continue
            top = _safe(c["name"])
            index = io.StringIO()
            w = csv.writer(index)
            w.writerow(["file in zip", "original name", "document type", "period", "financial year", "how it was filed",
                         "confidence", "uploaded at"])
            used: set[str] = set()
            for d in db.rows(conn, "SELECT * FROM documents WHERE client_id=? AND status IN ('auto','confirmed') "
                                   "ORDER BY fy, doc_type, period_start, id", (cid,)):
                if not d["stored_path"] or not Path(d["stored_path"]).exists():
                    continue
                fy_dir = F.fy_label(d["fy"]) if d["fy"] is not None else "Undated"
                label = DOC_TYPES.get(d["doc_type"] or "other", DOC_TYPES["other"])["label"]
                prefix = d["period_start"] or ""
                if d["period_end"] and d["period_end"] != d["period_start"]:
                    prefix += f" to {d['period_end']}"
                base = _safe(f"{prefix} {Path(d['filename']).name}".strip())
                arc = f"{top}/{_safe(fy_dir)}/{_safe(label)}/{base}"
                p = PurePosixPath(arc)
                stem, ext, k = str(p.with_suffix("")), p.suffix, 2
                while arc in used:
                    arc = f"{stem} ({k}){ext}"
                    k += 1
                used.add(arc)
                z.writestr(arc, storage.read(d["stored_path"]))
                w.writerow([arc, d["filename"], label, prefix, fy_dir,
                            "checked by a person" if d["status"] == "confirmed" else "auto-filed",
                            d["confidence"], d["uploaded_at"]])
                n += 1
            z.writestr(f"{top}/_index.csv", index.getvalue())
            reg = io.StringIO()
            rw = csv.writer(reg)
            rw.writerow(["compliance", "form", "period key", "financial year", "due", "statutory due", "status", "ack no", "filed on"])
            from .compliance import RULES_BY_CODE
            for o in db.rows(conn, "SELECT * FROM obligations WHERE client_id=? AND status!='not_tracked' ORDER BY due", (cid,)):
                r = RULES_BY_CODE.get(o["rule_code"])
                rw.writerow([r.name if r else o["rule_code"], r.form if r else "", o["period_key"], F.fy_label(o["fy"]),
                             o["due"], o["due_default"], o["status"], o["ack_no"] or "", o["filed_on"] or ""])
            z.writestr(f"{top}/_filings.csv", reg.getvalue())
            profile = {k: c[k] for k in ("name", "pan", "gstin", "tan", "entity_type", "gst_scheme", "gst_state")}
            z.writestr(f"{top}/_profile.json", json.dumps(profile, indent=2))
    return n
