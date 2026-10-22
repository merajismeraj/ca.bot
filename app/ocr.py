"""Reading scanned documents and phone photos.

Engines, in order:
  1. Tesseract (local, free, nothing leaves the office) - images directly; for
     image-only PDFs, the page images embedded in the PDF.
  2. Claude vision - only if CABOT_VISION_OCR=1 and an API key is set, because
     it sends the file to Anthropic. Better on skewed photos and handwriting.

OCR runs in a background worker so a large dump returns immediately; each file
is re-classified as soon as its text is available.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import db

log = logging.getLogger("cabot.ocr")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
MAX_PAGES = 4


def needs_ocr(path: Path, text: str) -> bool:
    suffix = path.suffix.lower()
    return suffix in IMAGE_SUFFIXES or (suffix == ".pdf" and len(text.strip()) < 30)


def tesseract_available() -> bool:
    return shutil.which(os.environ.get("CABOT_TESSERACT", "tesseract")) is not None


def vision_enabled() -> bool:
    from .brain import llm_available
    return os.environ.get("CABOT_VISION_OCR", "0") == "1" and llm_available()


def available() -> bool:
    return tesseract_available() or vision_enabled()


def _langs() -> str:
    want = os.environ.get("CABOT_OCR_LANGS")
    if want:
        return want
    try:
        have = subprocess.run([os.environ.get("CABOT_TESSERACT", "tesseract"), "--list-langs"],
                              capture_output=True, text=True, timeout=10).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        return "eng"
    return "+".join(l for l in ("eng", "hin") if l in have) or "eng"


def _prepare(img):
    """Grayscale and upscale small phone images - roughly doubles Tesseract accuracy on WhatsApp photos."""
    from PIL import ImageOps
    img = ImageOps.exif_transpose(img).convert("L")
    if img.width < 1600:
        f = 1600 / max(img.width, 1)
        img = img.resize((int(img.width * f), int(img.height * f)))
    return img


def _tesseract_image(img) -> str:
    buf = io.BytesIO()
    _prepare(img).save(buf, format="PNG")
    p = subprocess.run([os.environ.get("CABOT_TESSERACT", "tesseract"), "stdin", "stdout", "-l", _langs(), "--psm", "3"],
                       input=buf.getvalue(), capture_output=True, timeout=120)
    return p.stdout.decode("utf-8", errors="ignore")


def page_images(path: Path) -> list:
    from PIL import Image
    if path.suffix.lower() in IMAGE_SUFFIXES:
        img = Image.open(path)
        frames = []
        for i in range(min(getattr(img, "n_frames", 1), MAX_PAGES)):  # multi-page TIFF from office scanners
            img.seek(i)
            frames.append(img.copy())
        return frames
    from pypdf import PdfReader
    out = []
    for page in PdfReader(str(path)).pages[:MAX_PAGES]:
        imgs = sorted(page.images, key=lambda im: im.image.width * im.image.height, reverse=True)
        if imgs:
            out.append(imgs[0].image)  # the scan is the largest image on the page
    return out


def tesseract(path: Path) -> str:
    return "\n".join(_tesseract_image(im) for im in page_images(path)).strip()


def vision(path: Path) -> str:
    import anthropic
    from .brain import MODEL

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.b64encode(path.read_bytes()).decode()}}
    else:
        from PIL import Image
        img = Image.open(path).convert("RGB")
        img.thumbnail((2000, 2000))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        block = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.b64encode(buf.getvalue()).decode()}}
    resp = anthropic.Anthropic().beta.messages.create(
        model=os.environ.get("CABOT_OCR_MODEL", MODEL), max_tokens=8000,
        output_config={"effort": "low"}, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        messages=[{"role": "user", "content": [block, {"type": "text", "text":
                   "Transcribe all text in this document exactly as printed, keeping line breaks. Include names, "
                   "PAN, GSTIN, dates, periods and headings. Output only the transcription."}]}])
    if resp.stop_reason == "refusal":
        return ""
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def read(path: Path) -> tuple[str, str]:
    """Return (text, engine). Empty text means nothing could be read."""
    from . import storage
    with storage.plain(path) as p:
        return _read(p)


def _read(path: Path) -> tuple[str, str]:
    text, engine = "", ""
    if tesseract_available():
        try:
            text, engine = tesseract(path), "tesseract"
        except Exception as e:  # corrupt image, exotic PDF filter
            log.warning("tesseract failed on %s: %s", path.name, e)
    if len(text.strip()) < 30 and vision_enabled():
        try:
            text, engine = vision(path), "claude"
        except Exception as e:
            log.warning("vision OCR failed on %s: %s", path.name, e)
    return text, engine


# ------------------------------------------------------------------ worker
def process_pending(limit: int = 10) -> int:
    from . import engine

    with db.tx() as c:
        todo = db.rows(c, "SELECT id, stored_path FROM documents WHERE ocr_status='pending' ORDER BY id LIMIT ?", (limit,))
        for d in todo:
            c.execute("UPDATE documents SET ocr_status='running' WHERE id=?", (d["id"],))
    for d in todo:
        text, eng = ("", "")
        if available():
            text, eng = read(Path(d["stored_path"]))
        with db.tx() as c:
            if len(text.strip()) >= 15:
                engine.reclassify(c, d["id"], text, f"Read from the scan ({eng})")
                c.execute("UPDATE documents SET ocr_status='done' WHERE id=?", (d["id"],))
            else:
                why = "Couldn't read any text from the scan" if available() else "No OCR engine installed"
                row = db.row(c, "SELECT reasons FROM documents WHERE id=?", (d["id"],))
                reasons = [r for r in json.loads(row["reasons"] or "[]") if not r.startswith("Reading")]
                c.execute("UPDATE documents SET ocr_status='failed', reasons=? WHERE id=?",
                          (json.dumps([why] + reasons), d["id"]))
    return len(todo)


def start_worker() -> None:
    if os.environ.get("CABOT_WORKER", "1") == "0":
        return
    with db.tx() as c:  # files interrupted mid-read by a restart
        c.execute("UPDATE documents SET ocr_status='pending' WHERE ocr_status='running'")

    def loop():
        while True:
            try:
                if not process_pending():
                    time.sleep(2)
            except Exception:
                log.exception("OCR worker error")
                time.sleep(10)

    threading.Thread(target=loop, name="ocr-worker", daemon=True).start()
