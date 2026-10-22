"""Compliance calendar engine.

Every statutory obligation is a `Rule`: who it applies to, which periods it
recurs for in a financial year, its statutory due date, and the documents a
preparer needs before work can start. Rules are pure functions of the client
profile and FY, so the whole FY calendar for any client can be regenerated at
any time; status and partner overrides live in the database, not here.

Due dates are the statutory defaults. Government extensions (CBDT / CBIC / MCA
circulars) are applied as due-date overrides that a partner approves - they are
never assumed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from . import fy as F
from .catalog import QRMP_22ND_STATES


@dataclass(frozen=True)
class Instance:
    period_key: str          # stable id within the FY, e.g. "2026-04", "Q2", "FY"
    months: tuple[str, ...]  # months the obligation is about
    due: date


@dataclass(frozen=True)
class Req:
    doc_type: str
    scope: str = "period"    # "period" | "fy" | "ytd"


@dataclass(frozen=True)
class Rule:
    code: str
    name: str
    authority: str
    form: str
    applies: Callable[[dict], bool]
    instances: Callable[[int, dict], list[Instance]]
    reqs: Callable[[dict], list[Req]]
    depends_on: tuple[str, ...] = ()
    help: str = ""
    penalty: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------- predicates
def is_business(c: dict) -> bool:
    return c["entity_type"] in ("firm", "llp", "company", "trust") or bool(
        c.get("gst_scheme", "none") != "none" or c.get("tax_audit") or c.get("presumptive"))


def gst_regular(c: dict) -> bool:
    return c.get("gst_scheme") in ("monthly", "qrmp")


def flag(name: str) -> Callable[[dict], bool]:
    return lambda c: bool(c.get(name))


# ---------------------------------------------------------- instance makers
def monthly(day: int, march_override: tuple[int, int] | None = None):
    """Due on `day` of the month following each FY month."""
    def make(fy: int, c: dict) -> list[Instance]:
        out = []
        for mk in F.fy_months(fy):
            y, m = F.next_month(mk)
            due = F.safe_date(y, m, day)
            if march_override and mk.endswith("-03"):
                due = F.safe_date(fy + 1, march_override[0], march_override[1])
            out.append(Instance(mk, (mk,), due))
        return out
    return make


def quarterly(day: int, q4: tuple[int, int] | None = None, offset_months: int = 1):
    """Due on `day` of the month `offset_months` after each quarter ends."""
    def make(fy: int, c: dict) -> list[Instance]:
        out = []
        for q in range(1, 5):
            months = F.quarter_months(fy, q)
            y, m = F.parse_month_key(months[-1])
            for _ in range(offset_months):
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            due = F.safe_date(y, m, day)
            if q == 4 and q4:
                due = F.safe_date(fy + 1, q4[0], q4[1])
            out.append(Instance(f"Q{q}", tuple(months), due))
        return out
    return make


def annual(month: int, day: int, year_offset: int = 1):
    """One instance for the FY, due on a fixed date (year = fy + year_offset)."""
    def make(fy: int, c: dict) -> list[Instance]:
        return [Instance("FY", tuple(F.fy_months(fy)), F.safe_date(fy + year_offset, month, day))]
    return make


def qrmp_3b(fy: int, c: dict) -> list[Instance]:
    day = 22 if (c.get("gst_state") or "") in QRMP_22ND_STATES else 24
    return quarterly(day)(fy, c)


def qrmp_pmt06(fy: int, c: dict) -> list[Instance]:
    """PMT-06 challan for the first two months of each quarter, due the 25th."""
    out = []
    for q in range(1, 5):
        for mk in F.quarter_months(fy, q)[:2]:
            y, m = F.next_month(mk)
            out.append(Instance(mk, (mk,), F.safe_date(y, m, 25)))
    return out


def advance_tax(fy: int, c: dict) -> list[Instance]:
    if c.get("presumptive"):
        return [Instance("I4", tuple(F.fy_months(fy)), date(fy + 1, 3, 15))]
    # (instalment, months covered so far, due) - cumulative 15/45/75/100 %
    plan = [("I1", 3, date(fy, 6, 15)), ("I2", 6, date(fy, 9, 15)),
            ("I3", 9, date(fy, 12, 15)), ("I4", 12, date(fy + 1, 3, 15))]
    return [Instance(k, tuple(F.fy_months(fy)[:n]), d) for k, n, d in plan]


def itr_instances(fy: int, c: dict) -> list[Instance]:
    if c.get("transfer_pricing"):
        due = date(fy + 1, 11, 30)
    elif c.get("tax_audit") or c["entity_type"] == "company":
        due = date(fy + 1, 10, 31)
    else:
        due = date(fy + 1, 7, 31)
    return [Instance("FY", tuple(F.fy_months(fy)), due)]


def itr_reqs(c: dict) -> list[Req]:
    reqs = [Req("ais_26as", "fy"), Req("bank_statement", "fy")]
    if c["entity_type"] in ("individual", "huf"):
        reqs.append(Req("investment_proofs", "fy"))
        if c.get("salaried"):
            reqs.append(Req("form16", "fy"))
    if is_business(c):
        reqs.append(Req("financials", "fy"))
    return reqs


def const(*reqs: Req) -> Callable[[dict], list[Req]]:
    return lambda c: list(reqs)


# --------------------------------------------------------------------- rules
RULES: list[Rule] = [
    # ------------------------------------------------------------------ GST
    Rule("GSTR1_M", "GSTR-1 (monthly)", "GST", "GSTR-1",
         lambda c: c.get("gst_scheme") == "monthly", monthly(11),
         const(Req("sales_register")), help="Outward supplies return.",
         penalty="Late fee ₹50/day (₹20/day nil), capped."),
    Rule("GSTR3B_M", "GSTR-3B (monthly)", "GST", "GSTR-3B",
         lambda c: c.get("gst_scheme") == "monthly", monthly(20),
         const(Req("sales_register"), Req("purchase_register"), Req("gstr2b")),
         help="Summary return + tax payment.", penalty="Late fee + 18% p.a. interest on cash liability."),
    Rule("GSTR1_Q", "GSTR-1 (QRMP quarterly)", "GST", "GSTR-1",
         lambda c: c.get("gst_scheme") == "qrmp", quarterly(13),
         const(Req("sales_register")), help="Quarterly outward supplies (QRMP)."),
    Rule("PMT06", "GST PMT-06 monthly payment (QRMP)", "GST", "PMT-06",
         lambda c: c.get("gst_scheme") == "qrmp", qrmp_pmt06,
         const(Req("sales_register"), Req("purchase_register")),
         help="Tax deposit for months 1 & 2 of the quarter.", penalty="18% p.a. interest."),
    Rule("GSTR3B_Q", "GSTR-3B (QRMP quarterly)", "GST", "GSTR-3B",
         lambda c: c.get("gst_scheme") == "qrmp", qrmp_3b,
         const(Req("sales_register"), Req("purchase_register"), Req("gstr2b")),
         help="Due 22nd or 24th depending on the state of registration."),
    Rule("CMP08", "CMP-08 (composition)", "GST", "CMP-08",
         lambda c: c.get("gst_scheme") == "composition", quarterly(18),
         const(Req("sales_register")), help="Quarterly composition tax statement."),
    Rule("GSTR4", "GSTR-4 annual (composition)", "GST", "GSTR-4",
         lambda c: c.get("gst_scheme") == "composition", annual(4, 30),
         const(Req("sales_register", "fy"), Req("purchase_register", "fy")), depends_on=("CMP08",)),
    Rule("GSTR9", "GSTR-9 annual return", "GST", "GSTR-9",
         lambda c: gst_regular(c) and (c.get("turnover_cr") or 0) > 2, annual(12, 31),
         const(Req("sales_register", "fy"), Req("purchase_register", "fy")),
         depends_on=("GSTR1_M", "GSTR3B_M", "GSTR1_Q", "GSTR3B_Q"),
         help="Mandatory above ₹2 Cr aggregate turnover."),
    Rule("GSTR9C", "GSTR-9C reconciliation", "GST", "GSTR-9C",
         lambda c: gst_regular(c) and (c.get("turnover_cr") or 0) > 5, annual(12, 31),
         const(Req("financials", "fy")), depends_on=("GSTR9",),
         help="Self-certified reconciliation above ₹5 Cr."),
    # ----------------------------------------------------------- TDS / TCS
    Rule("TDS_PAY", "TDS / TCS deposit", "Income Tax", "TDS deposit",
         flag("tds"), monthly(7, march_override=(4, 30)),
         lambda c: [Req("expense_ledger")] + ([Req("salary_register")] if c.get("payroll") else []),
         help="March deduction due 30 April.", penalty="Interest 1.5% per month from deduction date."),
    Rule("TDS_RET_SAL", "TDS statement – salary (24Q)", "Income Tax", "TDS statement (salary)",
         lambda c: bool(c.get("tds") and c.get("payroll")), quarterly(31, q4=(5, 31)),
         const(Req("salary_register"), Req("tds_challan")),
         help="Quarterly; Q4 by 31 May. Verify form number under the Income-tax Act, 2025.",
         penalty="₹200/day late fee, up to TDS amount."),
    Rule("TDS_RET_NONSAL", "TDS statement – non-salary (26Q)", "Income Tax", "TDS statement (non-salary)",
         flag("tds"), quarterly(31, q4=(5, 31)),
         const(Req("expense_ledger"), Req("tds_challan")),
         help="Quarterly; Q4 by 31 May.", penalty="₹200/day late fee, up to TDS amount."),
    Rule("TCS_RET", "TCS statement (27EQ)", "Income Tax", "TCS statement",
         flag("tcs"), quarterly(15, q4=(5, 15)), const(Req("sales_register"))),
    Rule("FORM16", "Issue Form 16 to employees", "Income Tax", "Form 16",
         lambda c: bool(c.get("tds") and c.get("payroll")), annual(6, 15),
         const(), depends_on=("TDS_RET_SAL",)),
    # ------------------------------------------------------------ Income tax
    Rule("ADV_TAX", "Advance tax instalment", "Income Tax", "Advance tax",
         flag("advance_tax"), advance_tax,
         lambda c: [Req("bank_statement", "ytd")] + ([Req("trial_balance", "ytd")] if is_business(c) else []),
         help="Cumulative 15 / 45 / 75 / 100 %. Presumptive: 100 % by 15 March.",
         penalty="Interest for default / deferment."),
    Rule("TAX_AUDIT", "Tax audit report", "Income Tax", "Tax audit report",
         flag("tax_audit"), annual(9, 30),
         const(Req("financials", "fy"), Req("trial_balance", "fy"), Req("fixed_asset_register", "fy"),
               Req("bank_statement", "fy")),
         help="Statutory default 30 Sep; extensions applied as overrides.",
         penalty="0.5 % of turnover, max ₹1.5 lakh."),
    Rule("ITR", "Income-tax return", "Income Tax", "ITR",
         lambda c: True, itr_instances, itr_reqs,
         help="31 Jul (non-audit) / 31 Oct (audit, companies) / 30 Nov (transfer pricing).",
         penalty="Late fee up to ₹5,000 + interest."),
    # ------------------------------------------------------------------- ROC
    Rule("DPT3", "DPT-3 return of deposits", "ROC", "DPT-3",
         lambda c: c["entity_type"] == "company", annual(6, 30), const(Req("financials", "fy"))),
    Rule("DIR3KYC", "DIR-3 KYC of directors", "ROC", "DIR-3 KYC",
         lambda c: c["entity_type"] == "company", annual(9, 30), const()),
    Rule("ADT1", "ADT-1 auditor appointment", "ROC", "ADT-1",
         lambda c: c["entity_type"] == "company", annual(10, 14), const(Req("board_docs", "fy")),
         help="15 days from AGM (assumes AGM on 30 Sep)."),
    Rule("AOC4", "AOC-4 financial statements", "ROC", "AOC-4",
         lambda c: c["entity_type"] == "company", annual(10, 29),
         const(Req("financials", "fy"), Req("board_docs", "fy")),
         help="30 days from AGM (assumes AGM on 30 Sep).", penalty="₹100/day additional fee."),
    Rule("MGT7", "MGT-7 / 7A annual return", "ROC", "MGT-7",
         lambda c: c["entity_type"] == "company", annual(11, 28), const(Req("board_docs", "fy")),
         help="60 days from AGM.", penalty="₹100/day additional fee."),
    Rule("LLP11", "LLP Form 11 annual return", "ROC", "Form 11",
         lambda c: c["entity_type"] == "llp", annual(5, 30), const()),
    Rule("LLP8", "LLP Form 8 statement of accounts", "ROC", "Form 8",
         lambda c: c["entity_type"] == "llp", annual(10, 30), const(Req("financials", "fy"))),
    # --------------------------------------------------------------- Payroll
    Rule("PF", "PF ECR & payment", "Payroll", "ECR",
         flag("payroll"), monthly(15), const(Req("salary_register"))),
    Rule("ESI", "ESI contribution", "Payroll", "ESI",
         flag("payroll"), monthly(15), const(Req("salary_register"))),
]

RULES_BY_CODE = {r.code: r for r in RULES}


def required_months(req: Req, inst: Instance, fy: int) -> list[str]:
    if req.scope == "fy":
        return F.fy_months(fy)
    if req.scope == "ytd":
        return F.fy_months(fy)[: F.fy_months(fy).index(inst.months[-1]) + 1]
    return list(inst.months)


def generate(client: dict, fy: int) -> list[tuple[Rule, Instance]]:
    out = []
    for rule in RULES:
        if rule.applies(client):
            out.extend((rule, inst) for inst in rule.instances(fy, client))
    return out
