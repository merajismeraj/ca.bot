from datetime import date

from app.classifier import classify, learnable_tokens, match_period

CLIENTS = [
    {"id": 1, "name": "Sharma Traders", "pan": "ABCPS1234K", "gstin": "27ABCPS1234K1Z5", "aliases": ["RKS"]},
    {"id": 2, "name": "Mehta Textiles Pvt Ltd", "pan": "AACCM4321Q", "gstin": None, "aliases": []},
]


def run(path, text="", **kw):
    return classify(path, text, CLIENTS, kw.get("lc", {}), kw.get("lt", {}), today=date(2026, 10, 22))


def test_gstin_in_content_wins():
    r = run("random.xlsx", "Sales Register Apr 2026\nGSTIN: 27ABCPS1234K1Z5")
    assert r.client_id == 1 and r.client_conf >= 0.99
    assert r.doc_type == "sales_register" and r.period_start == "2026-04"


def test_name_in_path_and_month_formats():
    r = run("Sharma Traders/Bank/HDFC stmt Sep-2026.pdf")
    assert (r.client_id, r.doc_type, r.period_start, r.status) == (1, "bank_statement", "2026-09", "auto")
    assert match_period("x_2026-07.csv", "", "sales_register")[:2] == ("2026-07", "2026-07")
    assert match_period("purchase 08-2026.csv", "", "purchase_register")[:2] == ("2026-08", "2026-08")


def test_fy_ay_quarter():
    assert run("Form 26AS AY 2026-27.pdf", "PAN ABCPS1234K").period_start == "2025-04"
    r = run("Mehta TDS challan Q2 FY 2026-27.pdf")
    assert (r.period_start, r.period_end) == ("2026-07", "2026-09")
    assert run("Audited financial statements FY 2025-26.pdf").doc_type == "financials"


def test_statement_period_from_content():
    r = run("download.csv", "Account Name: MEHTA TEXTILES PVT LTD\nStatement Period: 01/06/2026 to 30/06/2026\n")
    assert r.client_id == 2 and r.period_start == "2026-06"


def test_unknown_goes_to_review_and_learning_helps():
    r = run("WhatsApp Image 2026-09-14.jpeg")
    assert r.status == "needs_review" and r.client_id is None
    toks = learnable_tokens("vk_bank_stmt_apr26.csv", ["Sharma Traders"])
    assert "vk" not in toks  # too short to be safe
    toks = learnable_tokens("vkent_salesbook.csv", ["Sharma Traders"])
    assert "vkent" in toks
    r2 = run("vkent_salesbook june 2026.csv", lc={"vkent": 2})
    assert r2.client_id == 2
