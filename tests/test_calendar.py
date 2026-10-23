from datetime import date

from app import fy as F
from app.compliance import RULES_BY_CODE, generate


def client(**kw):
    base = {"entity_type": "individual", "gst_scheme": "none", "turnover_cr": 0}
    return base | kw


def dues(c, code, fy=2026):
    return {i.period_key: i.due for r, i in generate(c, fy) if r.code == code}


def test_fy_helpers():
    assert F.fy_of(date(2026, 3, 31)) == 2025 and F.fy_of(date(2026, 4, 1)) == 2026
    assert F.fy_months(2026)[0] == "2026-04" and F.fy_months(2026)[-1] == "2027-03"
    assert F.quarter_months(2026, 4) == ["2027-01", "2027-02", "2027-03"]
    assert F.period_label(F.fy_months(2026), 2026) == "FY 2026-27"
    assert F.ay_label(2025) == "AY 2026-27"


def test_monthly_gst_dates():
    d = dues(client(gst_scheme="monthly", gst_state="27"), "GSTR3B_M")
    assert d["2026-09"] == date(2026, 10, 20) and d["2027-03"] == date(2027, 4, 20)
    assert dues(client(gst_scheme="monthly"), "GSTR1_M")["2026-12"] == date(2027, 1, 11)


def test_qrmp_3b_depends_on_state():
    assert dues(client(gst_scheme="qrmp", gst_state="27"), "GSTR3B_Q")["Q2"] == date(2026, 10, 22)
    assert dues(client(gst_scheme="qrmp", gst_state="07"), "GSTR3B_Q")["Q2"] == date(2026, 10, 24)
    assert set(dues(client(gst_scheme="qrmp"), "PMT06")) == {"2026-04", "2026-05", "2026-07", "2026-08", "2026-10", "2026-11", "2027-01", "2027-02"}


def test_tds_special_dates():
    c = client(tds=True, payroll=True)
    assert dues(c, "TDS_PAY")["2027-03"] == date(2027, 4, 30)
    assert dues(c, "TDS_PAY")["2026-10"] == date(2026, 11, 7)
    assert dues(c, "TDS_RET_SAL") == {"Q1": date(2026, 7, 31), "Q2": date(2026, 10, 31), "Q3": date(2027, 1, 31), "Q4": date(2027, 5, 31)}


def test_itr_due_depends_on_profile():
    assert dues(client(), "ITR", 2025)["FY"] == date(2026, 7, 31)
    assert dues(client(tax_audit=True), "ITR", 2025)["FY"] == date(2026, 10, 31)
    assert dues(client(entity_type="company"), "ITR", 2025)["FY"] == date(2026, 10, 31)
    assert dues(client(transfer_pricing=True), "ITR", 2025)["FY"] == date(2026, 11, 30)


def test_applicability_thresholds():
    small = client(gst_scheme="monthly", turnover_cr=1.5)
    assert not dues(small, "GSTR9")
    assert dues(client(gst_scheme="monthly", turnover_cr=3), "GSTR9")["FY"] == date(2027, 12, 31)
    assert not dues(client(gst_scheme="monthly", turnover_cr=3), "GSTR9C")
    assert dues(client(advance_tax=True, presumptive=True), "ADV_TAX") == {"I4": date(2027, 3, 15)}
    assert len(dues(client(advance_tax=True), "ADV_TAX")) == 4
    codes = {r.code for r, _ in generate(client(entity_type="company"), 2026)}
    assert {"AOC4", "MGT7", "ADT1", "DPT3", "DIR3KYC", "ITR"} <= codes
    assert "GSTR9" in RULES_BY_CODE
