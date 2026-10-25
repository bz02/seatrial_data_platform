"""Accounting task templates.

Every template is a pure function of a seed. It returns what the expert sees (the sandbox's
initial state) and a hidden answer key the verifier grades against. All companies, people and
numbers are synthetic, so the data belongs to us and contains no client information.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

VENDORS = [
    ("Northwind Office Supply", "billing@northwind-office.example"),
    ("Bluebird Logistics", "ar@bluebird-logistics.example"),
    ("Cascade Paper Co.", "invoices@cascade-paper.example"),
    ("Summit IT Services", "accounts@summit-it.example"),
    ("Redwood Janitorial", "billing@redwood-clean.example"),
    ("Harbor Electric", "ar@harbor-electric.example"),
    ("Pinecrest Catering", "orders@pinecrest-catering.example"),
    ("Granite Security", "billing@granite-security.example"),
]
CUSTOMERS = ["Acme Retail", "Lakeside Dental", "Orion Fitness", "Maple Grove HOA", "Vista Realty",
             "Juniper Labs", "Copperleaf Cafe", "Sterling Law"]
EMPLOYEES = ["J. Rivera", "M. Chen", "A. Patel", "S. Okafor", "L. Nguyen", "D. Kowalski"]
COMPANY = "Brightwater Design LLC"
CONTROLLER = "controller@brightwater-design.example"


def _money(x: float) -> float:
    return round(x + 0.0, 2)


def _col(key, label, type_="text", editable=False, options=None):
    c = {"key": key, "label": label, "type": type_, "editable": editable}
    if options:
        c["options"] = options
    return c


# ---------------------------------------------------------------------------
# 1. Bank reconciliation
# ---------------------------------------------------------------------------
BANK_CLASSES = ["cleared", "bank_fee", "nsf_return", "book_error", "outstanding_check", "deposit_in_transit"]


def bank_reconciliation(seed: int) -> dict:
    r = random.Random(seed)
    start = date(2026, 9, 1)
    begin_balance = _money(r.uniform(18000, 42000))

    ledger, bank = [], []
    check_no = r.randint(1040, 1900)

    def d(day):
        return (start + timedelta(days=day)).isoformat()

    # Common cleared items
    n_common = r.randint(9, 12)
    for i in range(n_common):
        day = r.randint(1, 26)
        if r.random() < 0.45:
            amt = _money(r.uniform(900, 7800))
            desc = f"Deposit - {r.choice(CUSTOMERS)}"
            ck = ""
        else:
            amt = -_money(r.uniform(120, 4200))
            vendor = r.choice(VENDORS)[0]
            check_no += 1
            ck = str(check_no)
            desc = f"Check {ck} - {vendor}"
        ledger.append({"date": d(day), "description": desc, "check_no": ck, "amount": amt})
        bank.append({"date": d(min(day + r.randint(0, 3), 29)), "description": desc.upper()[:38],
                     "amount": amt, "_pair": len(ledger) - 1, "_cls": "cleared"})

    # Book error: transposed digits on a cleared check. Bank shows the true amount.
    true_amt = r.choice([1254.00, 2318.00, 3672.00, 4518.00, 1436.00])
    s = f"{int(true_amt):04d}"
    booked = float(s[0] + s[2] + s[1] + s[3])
    check_no += 1
    vendor = r.choice(VENDORS)[0]
    ledger.append({"date": d(8), "description": f"Check {check_no} - {vendor}", "check_no": str(check_no),
                   "amount": -booked})
    bank.append({"date": d(11), "description": f"CHECK {check_no}", "amount": -true_amt,
                 "_pair": len(ledger) - 1, "_cls": "book_error"})

    # Bank-only items
    fee = _money(r.choice([15.0, 25.0, 35.0, 42.5]))
    bank.append({"date": d(29), "description": "MONTHLY SERVICE CHARGE", "amount": -fee, "_pair": None,
                 "_cls": "bank_fee"})
    nsf_cust = r.choice(CUSTOMERS)
    nsf = _money(r.uniform(400, 1900))
    bank.append({"date": d(18), "description": f"RETURNED ITEM NSF {nsf_cust.upper()}", "amount": -nsf,
                 "_pair": None, "_cls": "nsf_return"})

    # Book-only items
    check_no += 1
    oc_vendor = r.choice(VENDORS)[0]
    oc = -_money(r.uniform(300, 2600))
    ledger.append({"date": d(28), "description": f"Check {check_no} - {oc_vendor}", "check_no": str(check_no),
                   "amount": oc, "_cls": "outstanding_check"})
    dit = _money(r.uniform(1500, 6400))
    ledger.append({"date": d(29), "description": f"Deposit - {r.choice(CUSTOMERS)}", "check_no": "",
                   "amount": dit, "_cls": "deposit_in_transit"})

    # Shuffle bank lines by date, assign ids.
    r.shuffle(bank)
    bank.sort(key=lambda b: b["date"])
    for i, row in enumerate(ledger):
        row["id"] = f"L{i + 1:03d}"
    for i, row in enumerate(bank):
        row["id"] = f"B{i + 1:03d}"

    book_end = _money(begin_balance + sum(x["amount"] for x in ledger))
    bank_end = _money(begin_balance + sum(x["amount"] for x in bank))
    adj_bank = _money(bank_end + dit + oc)
    adj_book = _money(book_end - fee - nsf + (booked - true_amt))
    assert abs(adj_bank - adj_book) < 0.005, (adj_bank, adj_book)

    # Answer key
    cells = {"Bank Statement": {}, "General Ledger": {}}
    paired_ledger = set()
    for b in bank:
        lid = ledger[b["_pair"]]["id"] if b["_pair"] is not None else ""
        if lid:
            paired_ledger.add(lid)
        cells["Bank Statement"][b["id"]] = {"match_ledger_id": lid, "classification": b["_cls"]}
    for l in ledger:
        cls = l.get("_cls")
        if l["id"] in paired_ledger:
            cls = "book_error" if l["check_no"] == str(check_no - 1) and abs(l["amount"]) == booked else "cleared"
        cells["General Ledger"][l["id"]] = {"classification": cls}

    bank_rows = [{"id": b["id"], "date": b["date"], "description": b["description"], "amount": b["amount"],
                  "match_ledger_id": "", "classification": ""} for b in bank]
    ledger_rows = [{"id": l["id"], "date": l["date"], "description": l["description"], "check_no": l["check_no"],
                    "amount": l["amount"], "classification": ""} for l in ledger]

    state = {
        "sheets": [
            {"name": "Bank Statement",
             "columns": [_col("id", "Line"), _col("date", "Date", "date"), _col("description", "Description"),
                         _col("amount", "Amount", "money"),
                         _col("match_ledger_id", "Matches GL line", "text", True),
                         _col("classification", "Classification", "text", True, BANK_CLASSES)],
             "rows": bank_rows},
            {"name": "General Ledger",
             "columns": [_col("id", "Line"), _col("date", "Date", "date"), _col("description", "Description"),
                         _col("check_no", "Check #"), _col("amount", "Amount", "money"),
                         _col("classification", "Classification", "text", True, BANK_CLASSES)],
             "rows": ledger_rows},
        ],
        "fields": [
            {"key": "beginning_balance", "label": "Beginning balance (both)", "type": "money",
             "value": begin_balance, "readonly": True},
            {"key": "bank_ending_balance", "label": "Bank statement ending balance", "type": "money",
             "value": bank_end, "readonly": True},
            {"key": "book_ending_balance", "label": "GL cash ending balance", "type": "money",
             "value": book_end, "readonly": True},
            {"key": "adjusted_bank_balance", "label": "Adjusted bank balance", "type": "money", "value": None},
            {"key": "adjusted_book_balance", "label": "Adjusted book balance", "type": "money", "value": None},
            {"key": "book_adjustment_total", "label": "Total journal entry needed to GL cash", "type": "money",
             "value": None},
        ],
        "inbox": [{
            "id": "M1", "from": CONTROLLER, "to": "you@brightwater-design.example", "date": d(30),
            "subject": "September bank rec",
            "body": ("Hi - please reconcile the operating account for September before close. "
                     "Match every bank line to the GL where possible, classify every line on both "
                     "sheets, and give me the adjusted balances and the total entry we need to book. "
                     "Reply to me with a short summary of the reconciling items when you're done."),
        }],
        "outbox": [],
    }
    answer = {
        "cells": cells,
        "fields": {"adjusted_bank_balance": adj_bank, "adjusted_book_balance": adj_book,
                   "book_adjustment_total": _money(adj_book - book_end)},
        "emails": [{"to": CONTROLLER, "must_mention_any": [["service", "fee", "charge"], ["nsf", "returned"]]}],
        "weights": {"cells": 0.55, "fields": 0.35, "emails": 0.10},
    }
    brief = (f"You are the staff accountant at {COMPANY}. Reconcile the September operating account. "
             "On the Bank Statement sheet, enter the matching GL line for each bank line (leave blank if "
             "none) and classify every line. On the General Ledger sheet, classify every line. Fill in the "
             "adjusted balances and the total adjustment to GL cash, then email the controller a summary. "
             "Explain your reasoning out loud as you go, especially for every reconciling item.")
    return {"title": "Bank reconciliation - Brightwater Design, Sept 2026", "brief": brief,
            "difficulty": "medium", "reward_cents": 4500, "state": state, "answer": answer}


# ---------------------------------------------------------------------------
# 2. Accounts payable three-way match
# ---------------------------------------------------------------------------
AP_DECISIONS = ["approve", "hold"]
AP_REASONS = ["none", "price_variance", "qty_variance", "duplicate", "no_po", "math_error"]
PRICE_TOLERANCE = 0.02


def ap_three_way_match(seed: int) -> dict:
    r = random.Random(seed)
    items = ["Printer toner (case)", "Copy paper (pallet)", "Freight - regional", "Laptop docking station",
             "Monthly cleaning", "Electrical repair (hr)", "Event catering (head)", "Guard service (shift)"]
    pos, rrs, invs = [], [], []
    n = r.randint(9, 11)
    for i in range(n):
        v = r.randrange(len(VENDORS))
        po = {"id": f"PO-{4100 + i}", "vendor": VENDORS[v][0], "item": items[v], "qty": r.randint(4, 40),
              "unit_price": _money(r.uniform(18, 340))}
        pos.append(po)
        rrs.append({"id": f"RR-{7300 + i}", "po": po["id"], "qty_received": po["qty"],
                    "received_on": (date(2026, 9, 2) + timedelta(days=r.randint(0, 20))).isoformat()})

    kinds = ["none"] * (n - 5) + ["price_variance", "qty_variance", "math_error", "duplicate", "no_po"]
    r.shuffle(kinds)
    inv_no = r.randint(20100, 28000)
    expected = {}
    dup_source = None
    for i, (po, rr, kind) in enumerate(zip(pos, rrs, kinds)):
        inv_no += r.randint(3, 40)
        qty, price = po["qty"], po["unit_price"]
        if kind == "price_variance":
            price = _money(price * r.uniform(1.06, 1.15))
        if kind == "qty_variance":
            rr["qty_received"] = po["qty"] - r.randint(1, 3)
        total = _money(qty * price)
        if kind == "math_error":
            total = _money(total + r.choice([90, 100, 900, 1000]))
        po_ref = po["id"]
        if kind == "no_po":
            po_ref = ""
        inv = {"id": f"INV-{inv_no}", "vendor": po["vendor"], "po": po_ref, "qty": qty,
               "unit_price": price, "total": total,
               "vendor_email": dict(VENDORS)[po["vendor"]]}
        if kind == "duplicate":
            dup_source = len(invs)
            inv["_dup"] = True
            inv_ok = dict(inv)
            inv_ok.pop("_dup")
            invs.append(inv_ok)
            expected[inv_ok["id"]] = ("approve", "none")
            dup = dict(inv_ok)
            dup["id"] = inv_ok["id"] + "A"
            invs.append(dup)
            expected[dup["id"]] = ("hold", "duplicate")
            continue
        invs.append(inv)
        expected[inv["id"]] = ("approve", "none") if kind == "none" else ("hold", kind)

    # "no_po" invoices: drop the receiving report too, otherwise it would be odd.
    price_var = next(i for i in invs if expected[i["id"]] == ("hold", "price_variance"))

    inv_rows = [{"id": i["id"], "vendor": i["vendor"], "po": i["po"], "qty": i["qty"], "unit_price": i["unit_price"],
                 "total": i["total"], "decision": "", "reason": ""} for i in invs]
    cells = {"Invoices": {k: {"decision": v[0], "reason": v[1]} for k, v in expected.items()}}
    approved_total = _money(sum(i["total"] for i in invs if expected[i["id"]][0] == "approve"))

    state = {
        "sheets": [
            {"name": "Invoices",
             "columns": [_col("id", "Invoice"), _col("vendor", "Vendor"), _col("po", "PO"),
                         _col("qty", "Qty billed"), _col("unit_price", "Unit price", "money"),
                         _col("total", "Invoice total", "money"),
                         _col("decision", "Decision", "text", True, AP_DECISIONS),
                         _col("reason", "Hold reason", "text", True, AP_REASONS)],
             "rows": inv_rows},
            {"name": "Purchase Orders",
             "columns": [_col("id", "PO"), _col("vendor", "Vendor"), _col("item", "Item"), _col("qty", "Qty"),
                         _col("unit_price", "Unit price", "money")],
             "rows": pos},
            {"name": "Receiving",
             "columns": [_col("id", "Receipt"), _col("po", "PO"), _col("qty_received", "Qty received"),
                         _col("received_on", "Received", "date")],
             "rows": rrs},
        ],
        "fields": [
            {"key": "approved_total", "label": "Total approved for payment run", "type": "money", "value": None},
        ],
        "inbox": [{
            "id": "M1", "from": CONTROLLER, "to": "you@brightwater-design.example", "date": "2026-09-26",
            "subject": "Friday payment run",
            "body": ("Please three-way match this week's invoices against POs and receiving before the "
                     f"payment run. Our price tolerance is {int(PRICE_TOLERANCE * 100)}% per unit. Hold "
                     "anything that doesn't match and give the reason. For the price variance, email the "
                     "vendor and ask for a corrected invoice or credit memo, citing the invoice number."),
        }],
        "outbox": [],
    }
    answer = {
        "cells": cells,
        "fields": {"approved_total": approved_total},
        "emails": [{"to": price_var["vendor_email"], "must_mention_any": [[price_var["id"]],
                                                                          ["credit", "corrected", "revised"]]}],
        "weights": {"cells": 0.6, "fields": 0.25, "emails": 0.15},
    }
    brief = (f"You are the AP specialist at {COMPANY}. Three-way match each invoice against its purchase "
             "order and receiving report. Set every invoice to approve or hold, give a hold reason, enter "
             "the total approved for payment, and email the vendor with the price variance. Think out loud: "
             "say what you compare for each invoice and why you approve or hold it.")
    return {"title": "AP three-way match - weekly payment run", "brief": brief, "difficulty": "medium",
            "reward_cents": 4000, "state": state, "answer": answer}


# ---------------------------------------------------------------------------
# 3. Travel and expense audit
# ---------------------------------------------------------------------------
EXP_FLAGS = ["ok", "violation"]
EXP_CODES = ["none", "MEAL_LIMIT", "HOTEL_LIMIT", "ALCOHOL", "NO_RECEIPT", "CLASS_OF_TRAVEL", "LATE"]
POLICY = {"meal_per_person": 75.0, "hotel_per_night": 250.0, "receipt_over": 25.0, "submit_within_days": 60}


def expense_audit(seed: int) -> dict:
    r = random.Random(seed)
    submitted = date(2026, 9, 30)
    rows, expected = [], {}

    def add(category, amount, attendees, receipt, note, days_ago, code):
        rid = f"E{len(rows) + 1:02d}"
        rows.append({"id": rid, "employee": r.choice(EMPLOYEES),
                     "date": (submitted - timedelta(days=days_ago)).isoformat(), "category": category,
                     "amount": _money(amount), "attendees": attendees, "receipt": "Y" if receipt else "N",
                     "note": note, "flag": "", "policy_code": ""})
        expected[rid] = ("ok", "none") if code == "none" else ("violation", code)

    for _ in range(r.randint(6, 8)):
        kind = r.choice(["meal", "hotel", "air", "taxi"])
        if kind == "meal":
            ppl = r.randint(1, 4)
            add("Meals", r.uniform(18, 70) * ppl, ppl, True, r.choice(["Client lunch", "Team dinner", "Working lunch"]),
                r.randint(3, 40), "none")
        elif kind == "hotel":
            nights = r.randint(1, 3)
            add("Lodging", r.uniform(140, 240) * nights, 1, True, f"{nights} night(s), conference", r.randint(3, 40),
                "none")
        elif kind == "air":
            add("Airfare", r.uniform(220, 640), 1, True, "Economy, client site visit", r.randint(5, 45), "none")
        else:
            add("Ground transport", r.uniform(9, 24), 1, False, "Taxi to client", r.randint(2, 30), "none")

    ppl = r.randint(2, 3)
    add("Meals", (POLICY["meal_per_person"] + r.uniform(12, 40)) * ppl, ppl, True, "Client dinner", r.randint(5, 30),
        "MEAL_LIMIT")
    add("Lodging", r.uniform(290, 380), 1, True, "1 night, downtown hotel", r.randint(5, 30), "HOTEL_LIMIT")
    add("Meals", r.uniform(60, 140), 2, True, "Dinner incl. bottle of wine", r.randint(5, 30), "ALCOHOL")
    add("Supplies", r.uniform(40, 160), 1, False, "Presentation materials", r.randint(5, 30), "NO_RECEIPT")
    add("Airfare", r.uniform(1400, 2600), 1, True, "Business class upgrade, red-eye", r.randint(5, 30),
        "CLASS_OF_TRAVEL")
    add("Meals", r.uniform(20, 55), 1, True, "Working lunch", r.randint(75, 110), "LATE")

    r.shuffle(rows)
    for i, row in enumerate(rows):
        new_id = f"E{i + 1:02d}"
        expected[new_id + "_new"] = expected.pop(row["id"])
        row["id"] = new_id
    expected = {k.replace("_new", ""): v for k, v in expected.items()}
    reimbursable = _money(sum(x["amount"] for x in rows if expected[x["id"]][0] == "ok"))

    state = {
        "sheets": [{
            "name": "Expense Report",
            "columns": [_col("id", "Line"), _col("employee", "Employee"), _col("date", "Date", "date"),
                        _col("category", "Category"), _col("amount", "Amount", "money"),
                        _col("attendees", "Attendees"), _col("receipt", "Receipt"), _col("note", "Note"),
                        _col("flag", "Audit result", "text", True, EXP_FLAGS),
                        _col("policy_code", "Policy code", "text", True, EXP_CODES)],
            "rows": rows,
        }],
        "fields": [{"key": "total_reimbursable", "label": "Total reimbursable", "type": "money", "value": None}],
        "inbox": [{
            "id": "M1", "from": CONTROLLER, "to": "you@brightwater-design.example", "date": "2026-09-30",
            "subject": "Q3 T&E audit + policy",
            "body": ("Please audit the attached expense report against policy: meals up to "
                     f"${POLICY['meal_per_person']:.0f} per person; lodging up to ${POLICY['hotel_per_night']:.0f} "
                     "per night; no alcohol; itemized receipt required for anything over "
                     f"${POLICY['receipt_over']:.0f}; economy airfare only; expenses must be submitted within "
                     f"{POLICY['submit_within_days']} days. Report was submitted {submitted.isoformat()}. "
                     "Flag violations with the policy code; violating lines are not reimbursed at all."),
        }],
        "outbox": [],
    }
    answer = {"cells": {"Expense Report": {k: {"flag": v[0], "policy_code": v[1]} for k, v in expected.items()}},
              "fields": {"total_reimbursable": reimbursable}, "emails": [],
              "weights": {"cells": 0.7, "fields": 0.3, "emails": 0.0}}
    brief = (f"You are the T&E auditor at {COMPANY}. Audit every expense line against the policy in the "
             "controller's email, mark each line ok or violation with a policy code, and enter the total "
             "reimbursable. Narrate the rule you apply to each line as you go.")
    return {"title": "Travel & expense audit - Q3", "brief": brief, "difficulty": "easy", "reward_cents": 3000,
            "state": state, "answer": answer}


TEMPLATES = {
    "bank_reconciliation": bank_reconciliation,
    "ap_three_way_match": ap_three_way_match,
    "expense_audit": expense_audit,
}
