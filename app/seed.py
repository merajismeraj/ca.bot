"""Demo office: six realistic clients and a messy, half-complete file dump.

All files are synthetic and go through the same ingest pipeline a real upload
does, so the demo exercises classification, the review queue and readiness.
Run: python -m app.seed   (only into an empty data directory)
"""
from __future__ import annotations

import io
import random
from datetime import date, timedelta

from . import auth, db, engine
from . import fy as F

CLIENTS = [
    dict(name="Sharma Traders", pan="ABCPS1234K", gstin="27ABCPS1234K1Z5", entity_type="individual", gst_scheme="monthly",
         turnover_cr=3.2, tax_audit=True, tds=True, advance_tax=True, contact_name="Rajesh Sharma", phone="9820012345",
         email="rajesh@sharmatraders.in", aliases=["RKS"]),
    dict(name="Mehta Textiles Pvt Ltd", pan="AACCM4321Q", gstin="24AACCM4321Q1ZX", entity_type="company",
         gst_scheme="monthly", turnover_cr=12.5, tax_audit=True, tds=True, payroll=True, advance_tax=True,
         contact_name="Kavita Mehta", phone="9825098250", email="accounts@mehtatex.com"),
    dict(name="Iyer & Associates LLP", pan="AAJFI5678L", gstin="33AAJFI5678L1Z2", entity_type="llp", gst_scheme="qrmp",
         turnover_cr=1.1, tds=True, advance_tax=True, contact_name="S. Iyer", phone="9840098400"),
    dict(name="Dr. Anjali Rao", pan="BXRPR2345M", entity_type="individual", salaried=True, advance_tax=True,
         contact_name="Anjali", phone="9900099000", email="anjali.rao@example.com"),
    dict(name="Gupta Kirana Store", pan="CQGPG8765N", gstin="09CQGPG8765N1ZA", entity_type="individual",
         gst_scheme="composition", presumptive=True, advance_tax=True, contact_name="Mohan Gupta", phone="9415094150"),
    dict(name="Patel HUF", pan="AAAHP3456R", entity_type="huf", advance_tax=True, contact_name="Bharat Patel",
         phone="9879098790"),
]

rng = random.Random(7)


def bank_csv(holder: str, mk: str) -> bytes:
    y, m = F.parse_month_key(mk)
    start = date(y, m, 1)
    end = F.safe_date(y, m, 31)
    lines = ["STATEMENT OF ACCOUNT", f"Account Name: {holder}", "Account No: XXXXXX4521",
             f"Statement Period: {start:%d/%m/%Y} to {end:%d/%m/%Y}", "",
             "Txn Date,Narration,Debit,Credit,Balance"]
    bal = rng.randint(200000, 900000)
    d = start
    while d <= end:
        if rng.random() < 0.5:
            amt = rng.randint(2000, 90000)
            cr = rng.random() < 0.55
            bal += amt if cr else -amt
            lines.append(f"{d:%d/%m/%Y},{'NEFT CR' if cr else 'UPI DR'} {rng.choice(['ACME', 'RELIANCE', 'GST PMT', 'RENT', 'SALARY'])},"
                         f"{'' if cr else amt},{amt if cr else ''},{bal}")
        d += timedelta(days=1)
    return "\n".join(lines).encode()


def register_xlsx(kind: str, gstin: str, mk: str) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    y, m = F.parse_month_key(mk)
    ws.append([f"{kind} for {F.month_label(mk)}", f"GSTIN: {gstin}"])
    ws.append(["Invoice No", "Date", "Party GSTIN", "Taxable Value", "IGST", "CGST", "SGST"])
    for i in range(rng.randint(8, 20)):
        tv = rng.randint(5000, 250000)
        ws.append([f"INV/{y}/{m:02d}/{i + 1:03d}", f"{rng.randint(1, 28):02d}/{m:02d}/{y}", "29AAACX1234B1Z9", tv,
                   0, round(tv * 0.09, 2), round(tv * 0.09, 2)])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def annual_txt(title: str, who: str, pan: str, fy: int) -> bytes:
    return (f"{title}\n{who}\nPAN: {pan}\nFinancial Year {fy}-{str(fy + 1)[-2:]}\n"
            f"Assessment Year {fy + 1}-{str(fy + 2)[-2:]}\nAmounts in INR\n").encode()


def photo_of_register(client: dict, mk: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1240, 900), (250, 248, 240))
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=34)
    small = ImageFont.load_default(size=28)
    lines = [(f"PURCHASE REGISTER - {F.month_label(mk).upper()}", font), (client["name"].upper(), font),
             (f"GSTIN: {client['gstin']}", small), ("", small),
             ("Bill No      Supplier              Taxable     GST", small)]
    for i in range(8):
        lines.append((f"PB/{i + 101}      Supplier {chr(65 + i)} Traders     {rng.randint(10, 99)},{rng.randint(100, 999)}"
                       f"     {rng.randint(1, 9)},{rng.randint(100, 999)}", small))
    y = 60
    for text, f in lines:
        d.text((60, y), text, fill=(20, 20, 30), font=f)
        y += 60
    buf = io.BytesIO()
    img.rotate(1.2, expand=True, fillcolor=(250, 248, 240)).save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def noise_photo() -> bytes:
    from PIL import Image
    img = Image.new("RGB", (640, 480), (rng.randint(0, 255), 120, 90))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def files() -> list[tuple[str, bytes]]:
    t = engine.today()
    cur = F.fy_of(t)
    prev = cur - 1
    done_months = [m for m in F.fy_months(cur) if F.parse_month_key(m) < (t.year, t.month)]  # Apr..last month
    out: list[tuple[str, bytes]] = []
    sharma, mehta, iyer, rao, gupta, patel = CLIENTS

    # Sharma: diligent, but September purchases + statement still missing
    for mk in done_months:
        lab = F.month_label(mk).replace(" ", "-")
        if mk != done_months[-1]:
            out.append((f"Sharma Traders/Bank/HDFC stmt {lab}.csv", bank_csv("SHARMA TRADERS", mk)))
            out.append((f"Sharma Traders/GST/Purchase register {lab}.xlsx", register_xlsx("Purchase Register", sharma["gstin"], mk)))
        out.append((f"Sharma Traders/GST/Sales register {lab}.xlsx", register_xlsx("Sales Register", sharma["gstin"], mk)))
        out.append((f"Sharma Traders/GST/GSTR-2B {lab}.csv", f"GSTR-2B {F.month_label(mk)}\nGSTIN {sharma['gstin']}\n".encode()))
        out.append((f"Sharma Traders/TDS/vendor payments ledger {lab}.csv", f"Ledger {F.month_label(mk)}\n{sharma['gstin']}\n".encode()))
        out.append((f"Sharma Traders/TDS/TDS challan 281 {lab}.csv", f"Challan ITNS 281 {F.month_label(mk)}\nTAN MUMS12345A\nPAN {sharma['pan']}\n".encode()))
    for mk in F.fy_months(prev):
        out.append((f"Sharma Traders/FY{prev}-{str(prev + 1)[-2:]}/HDFC stmt {F.month_label(mk).replace(' ', '-')}.csv",
                    bank_csv("SHARMA TRADERS", mk)))
    out.append((f"Sharma Traders/FY{prev}-{str(prev + 1)[-2:]}/Balance Sheet and P&L FY {prev}-{str(prev + 1)[-2:]}.txt",
                annual_txt("Balance Sheet and Profit and Loss", "Sharma Traders", sharma["pan"], prev)))
    out.append((f"Sharma Traders/FY{prev}-{str(prev + 1)[-2:]}/Form 26AS AY {prev + 1}-{str(prev + 2)[-2:]}.txt",
                annual_txt("Form 26AS - Annual Tax Statement", "Sharma Traders", sharma["pan"], prev)))

    # Mehta: big company, mostly complete for GST; payroll registers in place
    for mk in done_months:
        lab = F.month_label(mk).replace(" ", "")
        out.append((f"MEHTA/{lab}/bank statement {lab}.csv", bank_csv("MEHTA TEXTILES PVT LTD", mk)))
        out.append((f"MEHTA/{lab}/sales_{lab}.xlsx", register_xlsx("Sales Register", mehta["gstin"], mk)))
        out.append((f"MEHTA/{lab}/purchase_{lab}.xlsx", register_xlsx("Purchase Register", mehta["gstin"], mk)))
        out.append((f"MEHTA/{lab}/gstr2b_{lab}.csv", f"GSTR-2B {mehta['gstin']} {mk}".encode()))
        out.append((f"MEHTA/{lab}/salary register {lab}.csv", f"Salary Register\nPAN {mehta['pan']}\n {mk}".encode()))
        out.append((f"MEHTA/{lab}/expense ledger {lab}.csv", f"Ledger {mehta['pan']} {mk}".encode()))
        out.append((f"MEHTA/{lab}/tds challan {lab}.csv", f"Challan 281 {mehta['pan']} {mk}".encode()))
    pl = f"{prev}-{str(prev + 1)[-2:]}"
    out.append((f"MEHTA/Annual/Audited financial statements FY {pl}.txt", annual_txt("Audited Financial Statements", "Mehta Textiles Pvt Ltd", mehta["pan"], prev)))
    out.append((f"MEHTA/Annual/AGM minutes FY {pl}.txt", annual_txt("Minutes of AGM", "Mehta Textiles Pvt Ltd", mehta["pan"], prev)))

    # Iyer: QRMP, only Q1 sent
    for mk in F.quarter_months(cur, 1):
        lab = F.month_label(mk).replace(" ", "_")
        out.append((f"iyer/sales_{lab}.csv", f"Outward supplies {iyer['gstin']} {mk}".encode()))
        out.append((f"iyer/purchase_{lab}.csv", f"Inward supplies {iyer['gstin']} {mk}".encode()))
        out.append((f"iyer/gstr2b_{lab}.csv", f"GSTR-2B {iyer['gstin']} {mk}".encode()))

    # Dr Rao: salaried, sends things in one go
    out.append((f"Anjali/Form16 FY {pl}.txt", annual_txt("FORM 16", "Dr. Anjali Rao", rao["pan"], prev)))
    out.append((f"Anjali/AIS {pl}.txt", annual_txt("Annual Information Statement", "Dr. Anjali Rao", rao["pan"], prev)))
    out.append((f"Anjali/80C 80D proofs FY {pl}.txt", annual_txt("LIC premium receipt, 80D health insurance", "Anjali Rao", rao["pan"], prev)))
    out.append((f"Anjali/Zerodha tax P&L FY {pl}.txt", annual_txt("Tax P&L - capital gains", "Anjali Rao", rao["pan"], prev)))

    # Gupta: composition, a couple of sales summaries
    for mk in F.quarter_months(cur, 1):
        out.append((f"Gupta Kirana/sales {F.month_label(mk)}.csv", f"Sales {gupta['gstin']} {mk}".encode()))

    # the usual mess: no names, no dates
    # a phone photo of Sharma's missing September purchase register - only readable with OCR
    out.append((f"WhatsApp Image {t:%Y-%m-%d} at 18.22.31.jpeg", photo_of_register(sharma, done_months[-1])))
    out.append(("IMG-20260918-WA0007.jpg", noise_photo()))
    out.append(("RKS_stmt.csv", bank_csv("R K S", done_months[-1]).replace(b"STATEMENT OF ACCOUNT", b"Txn list")))
    out.append(("scan0042.pdf", b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"))
    out.append(("Patel docs/investment proofs.txt", b"PPF passbook, ELSS statement\nPAN AAAHP3456R\n"))
    return out


def run() -> list[dict]:
    from .main import ClientIn, _create_client

    db.init()
    t = engine.today()
    cur = F.fy_of(t)
    with db.tx() as c:
        if db.rows(c, "SELECT id FROM clients LIMIT 1"):
            raise SystemExit("Data directory already has clients; demo not loaded.")
        from . import tenancy
        logins = []
        # Arjun (manager) sees every client; Priya (article) only the client accounts assigned to her
        for name, email, level in (("Arjun", "arjun@demo.local", "manager"), ("Priya", "priya@demo.local", "article")):
            if not db.row(c, "SELECT id FROM users WHERE email=?", (email,)):
                uid = c.execute("INSERT INTO users(name, role, email, level, client_access, perms) VALUES(?,?,?,?,?,NULL)",
                                (name, auth.role_for(level), email, level, auth.LEVELS[level][2])).lastrowid
                tenancy.add_membership(email, db.current_firm(), uid)
                logins.append({"name": name, "email": email, "level": auth.LEVELS[level][0]})
        db.set_setting(c, "tracking_start", date(cur, 4, 1).isoformat())
        partner = db.row(c, "SELECT * FROM users WHERE level='owner' AND active=1 ORDER BY id LIMIT 1")
        staff = db.rows(c, "SELECT * FROM users WHERE email LIKE '%@demo.local' AND level!='owner' ORDER BY id") or [partner]
        for i, spec in enumerate(CLIENTS):
            spec = dict(spec, assignee_id=staff[i % len(staff)]["id"])
            _create_client(c, ClientIn(**spec), partner)
        for rel, blob in files():
            engine.ingest(c, rel, blob, staff[0], "demo")
        engine.folder_consensus(c, "demo")

        # history: everything due more than 3 days ago was filed, except a few realistic slips
        slips = {("Sharma Traders", "GSTR3B_M"), ("Iyer & Associates LLP", "GSTR3B_Q"), ("Gupta Kirana Store", "CMP08")}
        names = {r["id"]: r["name"] for r in db.rows(c, "SELECT id, name FROM clients")}
        for o in db.rows(c, "SELECT * FROM obligations WHERE status='open' AND due<? ORDER BY due",
                         ((t - timedelta(days=1)).isoformat(),)):
            last = o["due"] >= (t - timedelta(days=12)).isoformat()
            if last and (names[o["client_id"]], o["rule_code"]) in slips:
                continue
            filed = date.fromisoformat(o["due"]) - timedelta(days=rng.randint(0, 4))
            c.execute("UPDATE obligations SET status='filed', ack_no=?, filed_on=? WHERE id=?",
                      (f"AA{rng.randint(10**12, 10**13 - 1)}", filed.isoformat(), o["id"]))
        # live work in different stages
        upcoming = engine.enrich(c, db.rows(c, "SELECT * FROM obligations WHERE status='open' AND due>=? ORDER BY due",
                                            (t.isoformat(),)))
        ready = [o for o in upcoming if o["stage"] == "ready"]
        for o, st in zip(ready, ("review", "in_progress", "approved", "review")):
            c.execute("UPDATE obligations SET status=? WHERE id=?", (st, o["id"]))
        sharma = db.row(c, "SELECT id FROM clients WHERE name='Sharma Traders'")["id"]
        draft = engine.build_chase(c, sharma)
        if draft:
            c.execute("INSERT INTO messages(client_id,channel,subject,body,status,created_at) VALUES(?,?,?,?,'draft',?)",
                      (sharma, "whatsapp", draft["subject"], draft["body"], db.now()))
        db.log(c, partner["id"], "demo", "batch", None, "Demo office loaded")
    return logins


if __name__ == "__main__":
    import sys
    from . import tenancy
    owner = sys.argv[1] if len(sys.argv) > 1 else "neha@demo.local"
    tenancy.init()
    firm, _ = tenancy.create_firm("Kulkarni & Co., Chartered Accountants", owner, "CA Neha Kulkarni", None, "active")
    with db.use_firm(firm["id"]):
        team = run()
    print(f"Demo firm #{firm['id']} created. Super admin: {owner}")
    for l in team:
        print(f"  {l['level']:20} {l['email']}")
    print("Sign in with Google as one of these emails, or for local testing run with CABOT_DEV_LOGIN=1.")
