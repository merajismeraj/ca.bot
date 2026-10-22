"""Turns an anonymous file dump into (client, document type, period).

Signals, strongest first:
  client : GSTIN / PAN found in the file > folder or upload hint > learned alias >
           client-name tokens in the path
  type   : keywords in the filename > keywords in the content > learned tokens
  period : month/quarter/FY in the filename > "from .. to .." dates in content >
           span of dates in content

Every decision carries a confidence and a human-readable reason. Anything
below AUTO_THRESHOLD goes to the review queue; corrections made there are
written back as learned tokens so the same mistake is not asked twice.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import fy as F
from .catalog import DOC_TYPES

AUTO_THRESHOLD = 0.8

GSTIN_RE = re.compile(r"\b(\d{2})([A-Z]{5}\d{4}[A-Z])[1-9A-Z]Z[0-9A-Z]\b")
PAN_RE = re.compile(r"\b([A-Z]{3}[PCHFATBLJG][A-Z]\d{4}[A-Z])\b")
MONTH_NAMES = {m.lower(): i for i, m in enumerate(F.MONTHS, start=1)}
MONTH_NAMES.update({"january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7,
                    "august": 8, "september": 9, "sept": 9, "october": 10, "november": 11, "december": 12})
MONTH_ALT = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
NAME_STOP = {"pvt", "private", "ltd", "limited", "llp", "and", "the", "co", "m/s", "ms", "india", "enterprises",
             "company", "traders", "&", "sons", "huf"}
GENERIC_TOKENS = {"pdf", "xlsx", "xls", "csv", "txt", "doc", "docx", "copy", "final", "scan", "new", "file", "data",
                  "statement", "register", "report", "fy", "ay", "q1", "q2", "q3", "q4", "img", "scanned", "for", "of"}


@dataclass
class Result:
    client_id: int | None = None
    client_conf: float = 0.0
    doc_type: str = "other"
    type_conf: float = 0.0
    period_start: str | None = None
    period_end: str | None = None
    period_conf: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def fy(self) -> int | None:
        if not self.period_start:
            return None
        y, m = F.parse_month_key(self.period_start)
        return F.fy_of(date(y, m, 1))

    @property
    def confidence(self) -> float:
        return round(min(self.client_conf, self.type_conf, self.period_conf), 2)

    @property
    def status(self) -> str:
        return "auto" if self.confidence >= AUTO_THRESHOLD else "needs_review"


# ----------------------------------------------------------------- text
def extract_text(path: Path, limit: int = 20000) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix in (".csv", ".txt", ".tsv", ".json", ".xml"):
            return path.read_text(errors="ignore")[:limit]
        if suffix in (".xlsx", ".xlsm"):
            from openpyxl import load_workbook
            wb = load_workbook(path, read_only=True, data_only=True)
            buf = io.StringIO()
            w = csv.writer(buf)
            for ws in wb.worksheets[:3]:
                for i, r in enumerate(ws.iter_rows(values_only=True)):
                    if i > 400:
                        break
                    w.writerow(["" if v is None else v for v in r])
            return buf.getvalue()[:limit]
        if suffix == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            return "\n".join((p.extract_text() or "") for p in reader.pages[:5])[:limit]
    except Exception:  # unreadable or password-protected: classify on filename alone
        return ""
    return ""


def tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", s.lower()) if t]


def norm_name_tokens(name: str) -> list[str]:
    return [t for t in tokens(name) if t not in NAME_STOP and len(t) > 1]


# --------------------------------------------------------------- client
def match_client(path_str: str, text: str, clients: list[dict], learned: dict[str, int],
                 hint_client_id: int | None) -> tuple[int | None, float, str]:
    hay = f"{path_str}\n{text[:5000]}".upper()
    by_gstin = {c["gstin"]: c for c in clients if c.get("gstin")}
    by_pan = {c["pan"]: c for c in clients if c.get("pan")}

    found_gstins = {m.group(0) for m in GSTIN_RE.finditer(hay)}
    for g in found_gstins:
        if g in by_gstin:
            return by_gstin[g]["id"], 0.99, f"GSTIN {g} found in document"
    pans = {m.group(2) for m in GSTIN_RE.finditer(hay)} | {m.group(1) for m in PAN_RE.finditer(hay)}
    hits = {by_pan[p]["id"] for p in pans if p in by_pan}
    if len(hits) == 1:
        cid = hits.pop()
        pan = next(p for p in pans if p in by_pan and by_pan[p]["id"] == cid)
        return cid, 0.97, f"PAN {pan} found in document"

    if hint_client_id:
        return hint_client_id, 0.95, "Uploaded from the client's page"

    path_tokens = tokens(path_str)
    tokset = set(path_tokens)
    learned_hits = {learned[t] for t in tokset if t in learned}
    if len(learned_hits) == 1:
        return learned_hits.pop(), 0.9, "Matched a name you taught me earlier"

    joined = " ".join(path_tokens)
    best, best_score = None, 0.0
    for c in clients:
        names = [c["name"]] + list(c.get("aliases") or [])
        for n in names:
            nt = norm_name_tokens(n)
            if not nt:
                continue
            if " ".join(nt) in joined:
                score = 0.88
            else:
                overlap = sum(1 for t in nt if t in tokset)
                score = 0.55 * overlap / len(nt) if overlap else 0.0
            if score > best_score:
                best, best_score = c, score
    if best_score >= 0.8:
        return best["id"], best_score, "Client name in file path"
    # account-holder / party name printed inside the document
    head = " ".join(tokens(text[:3000]))
    for c in clients:
        for n in [c["name"]] + list(c.get("aliases") or []):
            nt = norm_name_tokens(n)
            if len(" ".join(nt)) >= 5 and f" {' '.join(nt)} " in f" {head} ":
                return c["id"], 0.86, "Client name printed inside the document"
    if best:
        return best["id"], round(best_score, 2), f"Partial name match ({best['name']})"
    return None, 0.0, "No PAN, GSTIN or client name found"


# ----------------------------------------------------------------- type
def kw_regex(kw: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(kw).replace(r"\ ", r"[\s_\-\.]*") + r"s?(?![a-z0-9])")


_KW = {t: [(kw, kw_regex(kw)) for kw in sorted(meta["kw"], key=len, reverse=True)] for t, meta in DOC_TYPES.items()}


def match_type(path_str: str, text: str, learned_types: dict[str, str]) -> tuple[str, float, str]:
    name = path_str.lower()
    best = ("other", 0.0, "")
    for dt, kws in _KW.items():
        for kw, rx in kws:
            if rx.search(name):
                score = 0.85 + min(len(kw), 15) / 100  # longer, more specific keywords win
                if score > best[1]:
                    best = (dt, score, f"“{kw}” in file name")
                break
    if best[1]:
        return best[0], min(round(best[1], 2), 0.97), best[2]
    for t in tokens(path_str):
        if t in learned_types:
            return learned_types[t], 0.88, "Matched a pattern you taught me earlier"
    head = text[:4000].lower()
    counts: dict[str, int] = {}
    for dt, kws in _KW.items():
        counts[dt] = sum(len(rx.findall(head)) for _, rx in kws)
    if counts and max(counts.values()) > 0:
        dt = max(counts, key=counts.get)
        return dt, 0.8 if counts[dt] >= 2 else 0.65, "Keywords inside the document"
    return "other", 0.3, "Could not tell what kind of document this is"


# --------------------------------------------------------------- period
def _year(y: str) -> int:
    v = int(y)
    return v + 2000 if v < 100 else v


FY_RE = re.compile(r"(?:fy|f\.y\.|financial year)?[\s_\-]*(20\d{2}|\d{2})\s*[-/_]\s*(20\d{2}|\d{2})(?!\d)", re.I)
AY_RE = re.compile(r"\bay[\s_\-]*(20\d{2})\s*[-/_]\s*(\d{2,4})", re.I)
Q_RE = re.compile(r"(?<![a-z0-9])q([1-4])(?![0-9])", re.I)
MON_YEAR_RE = re.compile(rf"(?<![a-z])({MONTH_ALT})(?![a-z])[\s_\-'\.,]*(20\d{{2}}|\d{{2}})(?!\d)", re.I)
NUM_MON_RE = re.compile(r"(?<!\d)(20\d{2})[-_.]?(0[1-9]|1[0-2])(?!\d)|(?<!\d)(0[1-9]|1[0-2])[-_.](20\d{2})(?!\d)")
DATE_RE = re.compile(rf"(?<!\d)(\d{{1,2}})[/\-. ]((?:0?[1-9]|1[0-2])|(?:{MONTH_ALT}))[/\-. ,]+(20\d{{2}}|\d{{2}})(?!\d)", re.I)
FROM_TO_RE = re.compile(r"(?:from|period)\s*:?\s*(.{6,20}?)\s+(?:to|till|-)\s+(.{6,20})", re.I)


def _valid_fy_pair(a: int, b: int) -> bool:
    return b == a + 1 or (b % 100) == (a + 1) % 100


def _parse_date(s: str) -> date | None:
    m = DATE_RE.search(s)
    if not m:
        return None
    d, mo, y = m.groups()
    mo_i = int(mo) if mo.isdigit() else MONTH_NAMES.get(mo.lower())
    try:
        return date(_year(y), mo_i, int(d))
    except (ValueError, TypeError):
        return None


def detect_fy(s: str) -> int | None:
    m = AY_RE.search(s)
    if m:
        return int(m.group(1)) - 1
    for m in FY_RE.finditer(s):
        a, b = _year(m.group(1)), _year(m.group(2))
        if 2000 <= a <= 2100 and _valid_fy_pair(a, b):
            return a
    return None


def match_period(path_str: str, text: str, doc_type: str, today: date | None = None) -> tuple[str | None, str | None, float, str]:
    name = Path(path_str).name
    # month in filename wins - most statements are named that way
    for src, label in ((name, "file name"), (path_str, "folder name")):
        months = []
        for m in MON_YEAR_RE.finditer(src):
            months.append(F.month_key(_year(m.group(2)), MONTH_NAMES[m.group(1).lower()]))
        for m in NUM_MON_RE.finditer(src):
            y, mo = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
            months.append(F.month_key(int(y), int(mo)))
        if months:
            months.sort()
            return months[0], months[-1], 0.92, f"Month in {label}"
        fy = detect_fy(src)
        q = Q_RE.search(src)
        if fy and q:
            qm = F.quarter_months(fy, int(q.group(1)))
            return qm[0], qm[-1], 0.92, f"Quarter in {label}"
        if fy:
            fm = F.fy_months(fy)
            return fm[0], fm[-1], 0.9, f"Financial year in {label}"

    head = text[:6000]
    m = FROM_TO_RE.search(head)
    if m:
        d1, d2 = _parse_date(m.group(1)), _parse_date(m.group(2))
        if d1 and d2 and d1 <= d2:
            return F.month_key(d1.year, d1.month), F.month_key(d2.year, d2.month), 0.9, "Statement period inside document"
    header_months = {F.month_key(_year(m.group(2)), MONTH_NAMES[m.group(1).lower()]) for m in MON_YEAR_RE.finditer(head[:600])}
    if len(header_months) == 1:
        mk = header_months.pop()
        return mk, mk, 0.85, "Month in document heading"
    fy = detect_fy(head)
    if fy and DOC_TYPES.get(doc_type, {}).get("gran") == "fy":
        fm = F.fy_months(fy)
        return fm[0], fm[-1], 0.85, "Financial year inside document"

    dates = []
    for dm in DATE_RE.finditer(text):
        d = _parse_date(dm.group(0))
        if d and 2015 <= d.year <= 2100:
            dates.append(d)
    if len(dates) >= 3:
        dates.sort()
        lo, hi = dates[0], dates[-1]
        span = (hi.year - lo.year) * 12 + hi.month - lo.month
        if span <= 12:
            return F.month_key(lo.year, lo.month), F.month_key(hi.year, hi.month), 0.82, "Transaction dates inside document"
        return F.month_key(lo.year, lo.month), F.month_key(hi.year, hi.month), 0.6, "Wide spread of dates – please check"
    if fy:
        fm = F.fy_months(fy)
        return fm[0], fm[-1], 0.7, "Financial year mentioned inside document"
    if today and DOC_TYPES.get(doc_type, {}).get("gran") == "fy":
        fm = F.fy_months(F.fy_of(today) - 1)
        return fm[0], fm[-1], 0.6, "Assumed last financial year – please check"
    return None, None, 0.0, "No period found"


# ------------------------------------------------------------------ main
def classify(path_str: str, text: str, clients: list[dict], learned_clients: dict[str, int],
             learned_types: dict[str, str], hint_client_id: int | None = None, today: date | None = None) -> Result:
    r = Result()
    r.client_id, r.client_conf, why_c = match_client(path_str, text, clients, learned_clients, hint_client_id)
    r.doc_type, r.type_conf, why_t = match_type(path_str, text, learned_types)
    r.period_start, r.period_end, r.period_conf, why_p = match_period(path_str, text, r.doc_type, today)
    r.reasons = [f"Client: {why_c}", f"Type: {why_t}", f"Period: {why_p}"]
    return r


def learnable_tokens(path_str: str, client_names: list[str]) -> list[str]:
    """Tokens from a filename worth remembering after a human correction."""
    name_tokens = {t for n in client_names for t in tokens(n)}
    skip = GENERIC_TOKENS | set(MONTH_NAMES) | name_tokens
    kw_tokens = {t for meta in DOC_TYPES.values() for kw in meta["kw"] for t in tokens(kw)}
    out = []
    for t in tokens(Path(path_str).stem) + tokens(str(Path(path_str).parent)):
        if len(t) >= 3 and not t.isdigit() and t not in skip and t not in kw_tokens and t not in out:
            out.append(t)
    return out[:3]
