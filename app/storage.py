"""Client files at rest, encrypted.

When CABOT_DATA_KEY is set (32 random bytes, base64), every uploaded file is
stored with AES-256-GCM. Files written before the key was set stay readable and
can be converted with:  python -m app.storage encrypt-all

The key never goes into the database or the data folder - keep it in the
server's secret store and back it up separately (lose it and the files are
unreadable; leak it together with a backup and the encryption is moot).
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

MAGIC = b"CABENC1\n"


def _key() -> bytes | None:
    k = os.environ.get("CABOT_DATA_KEY")
    if not k:
        return None
    raw = base64.b64decode(k)
    if len(raw) != 32:
        raise RuntimeError("CABOT_DATA_KEY must be 32 bytes, base64-encoded (python -m app.storage new-key)")
    return raw


def enabled() -> bool:
    return _key() is not None


def encrypt(data: bytes) -> bytes:
    key = _key()
    if not key:
        return data
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, MAGIC)


def decrypt(blob: bytes) -> bytes:
    if not blob.startswith(MAGIC):
        return blob  # stored before encryption was switched on
    key = _key()
    if not key:
        raise RuntimeError("This file is encrypted but CABOT_DATA_KEY is not set")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    n = len(MAGIC)
    return AESGCM(key).decrypt(blob[n:n + 12], blob[n + 12:], MAGIC)


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(encrypt(data))
    os.replace(tmp, path)


def read(path: str | Path) -> bytes:
    return decrypt(Path(path).read_bytes())


def is_encrypted(path: str | Path) -> bool:
    with open(path, "rb") as f:
        return f.read(len(MAGIC)) == MAGIC


@contextmanager
def plain(path: str | Path) -> Iterator[Path]:
    """A plaintext path for libraries that need a real file (pypdf, openpyxl, Tesseract). Temp copy is wiped after."""
    path = Path(path)
    if not is_encrypted(path):
        yield path
        return
    from . import db
    tmpdir = db.DATA_DIR / "tmp"
    tmpdir.mkdir(mode=0o700, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=path.suffix, dir=tmpdir)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(read(path))
        yield Path(name)
    finally:
        os.unlink(name)


def delete(path: str | Path | None) -> None:
    if path and Path(path).exists():
        Path(path).unlink()


def encrypt_all() -> int:
    from . import db
    if not enabled():
        raise SystemExit("Set CABOT_DATA_KEY first")
    n = 0
    with db.tx() as c:
        for d in db.rows(c, "SELECT stored_path FROM documents WHERE stored_path IS NOT NULL"):
            p = Path(d["stored_path"])
            if p.exists() and not is_encrypted(p):
                write(p, p.read_bytes())
                n += 1
    return n


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "new-key":
        print(base64.b64encode(os.urandom(32)).decode())
    elif cmd == "encrypt-all":
        print(f"Encrypted {encrypt_all()} files")
    else:
        print("usage: python -m app.storage new-key | encrypt-all")
