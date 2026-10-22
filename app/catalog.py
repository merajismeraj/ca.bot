"""Static vocabulary: document types, entity types, GST state codes."""
from __future__ import annotations

# doc_type -> (label, filename/content keywords, default granularity)
# granularity tells the classifier what a single file usually covers when it
# cannot find explicit dates: "month", "quarter" or "fy".
DOC_TYPES: dict[str, dict] = {
    "bank_statement": {"label": "Bank statement", "gran": "month",
                       "kw": ["bank statement", "statement of account", "account statement", "bank stmt", "stmt", "passbook", "hdfc", "icici", "sbi", "axis", "kotak"]},
    "sales_register": {"label": "Sales register / invoices", "gran": "month",
                       "kw": ["sales register", "sales", "outward", "invoice register", "sale bill", "tax invoice", "b2b", "b2c"]},
    "purchase_register": {"label": "Purchase register / bills", "gran": "month",
                          "kw": ["purchase register", "purchase", "inward", "vendor bill", "expenses bill"]},
    "gstr2b": {"label": "GSTR-2B", "gran": "month", "kw": ["gstr-2b", "gstr2b", "2b", "itc statement"]},
    "expense_ledger": {"label": "Expense / vendor ledger", "gran": "month",
                       "kw": ["ledger", "expense", "vendor payment", "payments", "contractor", "professional fees", "rent paid"]},
    "salary_register": {"label": "Salary register", "gran": "month",
                        "kw": ["salary", "payroll", "pay register", "wage", "payslip"]},
    "tds_challan": {"label": "TDS challan", "gran": "month", "kw": ["challan", "itns 281", "281", "tds paid", "tds payment"]},
    "pf_esi_challan": {"label": "PF / ESI challan", "gran": "month", "kw": ["ecr", "epf", "esic", "esi challan", "pf challan"]},
    "ais_26as": {"label": "AIS / 26AS / TIS", "gran": "fy", "kw": ["26as", "ais", "tis", "annual information", "form 26as", "form 168"]},
    "form16": {"label": "Form 16 / salary TDS certificate", "gran": "fy", "kw": ["form 16", "form16", "form-16", "form 130"]},
    "investment_proofs": {"label": "Investment / deduction proofs", "gran": "fy",
                          "kw": ["80c", "80d", "lic", "ppf", "elss", "insurance premium", "home loan certificate", "donation", "investment proof"]},
    "capital_gains": {"label": "Capital gains statement", "gran": "fy", "kw": ["capital gain", "p&l statement", "tax p&l", "zerodha", "groww", "cams", "kfintech"]},
    "trial_balance": {"label": "Trial balance / Tally export", "gran": "fy", "kw": ["trial balance", "tb", "tally", "daybook", "day book"]},
    "financials": {"label": "Financial statements", "gran": "fy",
                   "kw": ["balance sheet", "profit and loss", "p&l", "financial statement", "financials", "audited accounts"]},
    "fixed_asset_register": {"label": "Fixed asset register", "gran": "fy", "kw": ["fixed asset", "far", "depreciation", "asset register"]},
    "loan_statement": {"label": "Loan statement", "gran": "fy", "kw": ["loan statement", "loan account", "interest certificate", "emi"]},
    "board_docs": {"label": "Board / AGM documents", "gran": "fy", "kw": ["minutes", "board resolution", "agm", "notice of agm", "directors report"]},
    "other": {"label": "Other", "gran": "fy", "kw": []},
}

ENTITY_TYPES = {
    "individual": "Individual",
    "huf": "HUF",
    "firm": "Partnership firm",
    "llp": "LLP",
    "company": "Private / Public company",
    "trust": "Trust / Society",
}

# 4th PAN character -> entity type (Income-tax PAN structure)
PAN_ENTITY = {"P": "individual", "H": "huf", "F": "firm", "C": "company", "T": "trust", "A": "trust"}

GST_STATES = {
    "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
    "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura",
    "17": "Meghalaya", "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat", "25": "Daman & Diu",
    "26": "Dadra & Nagar Haveli", "27": "Maharashtra", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep",
    "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman & Nicobar", "36": "Telangana",
    "37": "Andhra Pradesh", "38": "Ladakh",
}

# QRMP GSTR-3B: these states file by the 22nd, everyone else by the 24th.
QRMP_22ND_STATES = {"22", "23", "24", "25", "26", "27", "29", "30", "31", "32", "33", "34", "35", "36", "37"}

ROLES = {"staff": "Staff", "partner": "Partner"}
