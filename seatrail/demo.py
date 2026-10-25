"""Demo data: organizations, users, tasks, and scripted episodes so every screen has content.

The scripted episodes are for demos and tests only. They are template-generated, not expert work,
and must never be sold or exported as training data.
"""
from __future__ import annotations

import random

from . import db, enterprise, experts, market, tasks
from .pipeline import quality

USERS = [
    # org 0: platform / marketplace
    (0, "admin", "Platform Admin", "admin@seatrail.example", ""),
    (0, "expert", "Dana Brooks, CPA", "dana@experts.example", "CPA, 12 yrs public accounting"),
    (0, "expert", "Luis Ortega, CPA", "luis@experts.example", "CPA, former AP manager"),
    (0, "buyer", "Frontier Lab (demo)", "data@frontier-lab.example", ""),
    (0, "seller", "Third-party Seller (demo)", "seller@vendor.example", ""),
    # org 1: an enterprise customer running its own deployment
    (1, "org_admin", "Priya Shah (IT admin)", "priya@halcyon.example", ""),
    (1, "steward", "Tom Reyes (data steward)", "tom@halcyon.example", ""),
    (1, "ml_engineer", "Mei Lin (ML engineer)", "mei@halcyon.example", ""),
    (1, "employee", "Sarah Kim (senior accountant)", "sarah@halcyon.example", "Senior accountant, AP"),
    (1, "employee", "Ben Adler (controller)", "ben@halcyon.example", "Controller"),
]


def seed_base() -> dict:
    now = db.now()
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO orgs (id, name, kind, policy, created_at) VALUES (0, 'SeaTrial Marketplace',"
                  " 'platform', '{}', ?)", (now,))
        policy = {**enterprise.DEFAULT_POLICY, "sensitive_terms": ["Halcyon", "Meridian Health", "Project Atlas"]}
        c.execute("INSERT OR IGNORE INTO orgs (id, name, kind, policy, created_at) VALUES (1, 'Halcyon Accounting Group',"
                  " 'enterprise', ?, ?)", (db.dumps(policy), now))
        for org, role, name, email, cred in USERS:
            c.execute("INSERT OR IGNORE INTO users (org_id, role, name, email, credentials, created_at)"
                      " VALUES (?,?,?,?,?,?)", (org, role, name, email, cred, now))
    for template in tasks.TEMPLATES:
        for seed in range(1, 5):
            tasks.create_task(template, seed)
    experts.ensure_qualification_tasks(tasks.TEMPLATES)
    users = {u["email"]: u for u in db.q("SELECT * FROM users")}
    # The two demo experts have already passed every qualification, the same way a real expert would.
    for email in ("dana@experts.example", "luis@experts.example"):
        uid = users[email]["id"]
        done = experts.qualified_templates(uid)
        for t in db.q("SELECT * FROM tasks WHERE purpose='qualification'"):
            if t["template"] not in done:
                experts.grade_qualification(run_sandbox_episode(uid, t["id"], seed=uid))
    return {u["email"]: u for u in db.q("SELECT * FROM users")}


# ---------------------------------------------------------------------------
# Scripted sandbox solutions with narration
# ---------------------------------------------------------------------------
def _why(template: str, sheet: str, row: dict, cols: dict, r: random.Random) -> str:
    rid = row["id"]
    amt = row.get("amount", row.get("total"))
    amt_s = f"{abs(amt):,.2f}" if isinstance(amt, (int, float)) else ""
    if template == "bank_reconciliation":
        cls = cols.get("classification")
        if sheet == "Bank Statement":
            m = cols.get("match_ledger_id")
            return {
                "cleared": r.choice([
                    f"{rid} for {amt_s} ties to {m} on the GL, same amount and the dates are within a few days, so it cleared.",
                    f"Matching {rid} against {m}: {amt_s} on both sides, bank posted it shortly after we booked it. Cleared.",
                    f"{m} is the book side of {rid}; amounts agree at {amt_s}. Marking cleared.",
                    f"Found {amt_s} in the ledger as {m}, description lines up with {rid}, so that's a cleared item."]),
                "book_error": f"{rid} is check clearing at {amt_s} but {m} in the GL shows the digits transposed; the bank amount is right, so this is a book error we correct with a journal entry.",
                "bank_fee": f"{rid} is the monthly service charge of {amt_s}; it's not in the GL, so it reduces the book balance.",
                "nsf_return": f"{rid} is a returned NSF item of {amt_s}; the customer's check bounced, so we reverse the deposit on the books and re-bill the customer.",
            }.get(cls, f"{rid} classified {cls}.")
        return {
            "cleared": r.choice([f"{rid} {amt_s} appears on the bank statement, cleared.",
                                 f"{rid} already matched to a bank line, cleared.",
                                 f"Ledger line {rid} for {amt_s} cleared the bank this month.",
                                 f"{rid}: saw it come through on the statement, so cleared."]),
            "book_error": f"{rid} was booked with transposed digits, {amt_s} instead of the cleared amount.",
            "outstanding_check": f"{rid} check for {amt_s} was written on the 28th and hasn't cleared the bank yet, so it's an outstanding check that reduces the bank balance.",
            "deposit_in_transit": f"{rid} deposit of {amt_s} was recorded on the last day and isn't on the statement, deposit in transit, add to bank balance.",
        }.get(cls, f"{rid} {cls}.")
    if template == "ap_three_way_match":
        reason = cols.get("reason")
        return {
            "none": f"{rid}: PO {row['po']} price and quantity match and receiving shows full quantity, total {amt_s} recomputes, approve.",
            "price_variance": f"{rid}: unit price {row['unit_price']:,.2f} is more than the 2% tolerance above PO {row['po']}, hold for price variance and ask the vendor for a credit memo.",
            "qty_variance": f"{rid}: billed {row['qty']} units but the receiving report shows fewer received on {row['po']}, hold for quantity variance.",
            "math_error": f"{rid}: {row['qty']} times {row['unit_price']:,.2f} doesn't equal the invoice total of {amt_s}, math error, hold.",
            "duplicate": f"{rid}: same vendor, PO, and amount as an invoice we're already paying, this is a duplicate, hold.",
            "no_po": f"{rid}: no PO referenced on the invoice so there's nothing to match against, hold until purchasing provides one.",
        }.get(reason, f"{rid} {reason}.")
    code = cols.get("policy_code")
    return {
        "none": f"{rid} {row['category']} {amt_s} for {row['attendees']} attendee(s) is within policy and receipt rule is met, ok.",
        "MEAL_LIMIT": f"{rid} is {amt_s} for {row['attendees']} people, that's over $75 per person, meal limit violation.",
        "HOTEL_LIMIT": f"{rid} lodging {amt_s} for one night exceeds the $250 nightly cap.",
        "ALCOHOL": f"{rid} note says wine was included; alcohol isn't reimbursable, violation.",
        "NO_RECEIPT": f"{rid} is {amt_s} with no receipt, over the $25 threshold, so it can't be reimbursed.",
        "CLASS_OF_TRAVEL": f"{rid} is a business class upgrade at {amt_s}; policy is economy only.",
        "LATE": f"{rid} is dated {row['date']}, more than 60 days before the submission date, late.",
    }.get(code, f"{rid} {code}.")


def scripted_sandbox_events(task_row: dict, narrate: float = 1.0, accuracy: float = 1.0,
                            seed: int = 0) -> list[dict]:
    """A plausible expert episode for a task: views, narrated edits, fields, email, submit."""
    r = random.Random(seed)
    state = db.loads(task_row["initial_state"]) if isinstance(task_row["initial_state"], str) else task_row["initial_state"]
    answer = db.loads(task_row["answer_key"]) if isinstance(task_row["answer_key"], str) else task_row["answer_key"]
    t = 0.0
    ev = []

    def add(type_, payload):
        nonlocal t
        t += r.uniform(4, 25)
        ev.append({"type": type_, "payload": payload, "ts": round(t, 1)})

    add("view", {"target": "brief"})
    add("view", {"target": "email:M1"})
    add("think", {"text": "Reading the controller's request first so I know exactly what's expected and which rules apply."})
    for sheet in state["sheets"]:
        add("view", {"target": f"sheet:{sheet['name']}"})
    sheets = {s["name"]: {row["id"]: row for row in s["rows"]} for s in state["sheets"]}
    for sheet_name, rows in answer["cells"].items():
        add("view", {"target": f"sheet:{sheet_name}"})
        for rid, cols in rows.items():
            row = sheets[sheet_name][rid]
            if r.random() < narrate:
                add("think", {"text": _why(task_row["template"], sheet_name, row, cols, r)})
            for col, val in cols.items():
                if r.random() > accuracy:
                    sheet = next(s for s in state["sheets"] if s["name"] == sheet_name)
                    opts = next(c for c in sheet["columns"] if c["key"] == col).get("options") or [""]
                    val = r.choice(opts)
                add("edit_cell", {"sheet": sheet_name, "row": rid, "column": col, "value": val})
    add("view", {"target": "fields"})
    for k, v in answer["fields"].items():
        if r.random() < narrate:
            add("think", {"text": f"Now the {k.replace('_', ' ')}: summing the items I classified gives {v:,.2f}; I double-check it ties out before entering."})
        add("set_field", {"field": k, "value": v})
    for e in answer["emails"]:
        terms = " and ".join(g[0] for g in e["must_mention_any"])
        add("think", {"text": f"Writing to {e['to']} to close the loop; I need to mention {terms}."})
        add("send_email", {"to": e["to"], "subject": "Follow-up",
                           "body": f"Hello, following up regarding {terms}. Please send a corrected invoice or credit "
                                   f"memo as appropriate. Summary: service fee and NSF returned item noted. Thanks."})
    add("think", {"text": "Everything is classified and the balances tie, submitting."})
    add("submit", {})
    return ev


def run_sandbox_episode(expert_id: int, task_id: int, **kw) -> int:
    from . import sandbox
    t = db.q1("SELECT * FROM tasks WHERE id=?", task_id)
    sid = db.x("INSERT INTO sessions (org_id, source, task_id, user_id, status, state, started_at)"
               " VALUES (0, 'sandbox', ?, ?, 'in_progress', ?, ?)", task_id, expert_id, t["initial_state"], db.now())
    state = db.loads(t["initial_state"])
    with db.tx() as c:
        for i, e in enumerate(scripted_sandbox_events(t, **kw), start=1):
            state = sandbox.apply(state, e["type"], e["payload"])
            c.execute("INSERT INTO events (session_id, seq, ts, type, payload) VALUES (?,?,?,?,?)",
                      (sid, i, e["ts"], e["type"], db.dumps(e["payload"])))
        c.execute("UPDATE sessions SET state=? WHERE id=?", (db.dumps(state), sid))
    return sid


# ---------------------------------------------------------------------------
# Scripted capture on the demo ERP
# ---------------------------------------------------------------------------
def scripted_capture_events(base_url: str, variant: int = 0) -> list[dict]:
    r = random.Random(variant)
    vendor = r.choice(["Cascade Paper Co.", "Summit IT Services", "Harbor Electric", "Bluebird Logistics"])
    inv = f"INV-{r.randint(30000, 39999)}"
    amount = f"{r.uniform(800, 9000):.2f}"
    po = f"PO-{r.randint(5000, 5999)}"
    page = lambda path, title: {"url": base_url + path, "title": title}
    ev, t = [], 0.0

    def add(type_, payload):
        nonlocal t
        t += r.uniform(2, 14)
        ev.append({"type": type_, "payload": payload, "ts": round(t, 1)})

    add("navigate", {"url": base_url + "/demo-erp#invoices", "page": page("/demo-erp#invoices", "AP Inbox - Halcyon ERP")})
    add("think", {"text": f"New invoice from {vendor} came in, {inv} for {amount}. First I check it has a PO, because we never enter non-PO invoices without approval."})
    add("click", {"target": {"tag": "button", "text": "New invoice"}, "page": page("/demo-erp#invoices", "AP Inbox - Halcyon ERP")})
    add("input", {"target": {"tag": "input", "label": "Vendor"}, "value": vendor, "page": page("/demo-erp#new", "New invoice")})
    add("input", {"target": {"tag": "input", "label": "Invoice number"}, "value": inv, "page": page("/demo-erp#new", "New invoice")})
    add("think", {"text": f"Invoice references {po}. I open the PO to compare unit price and quantity before entering the amount."})
    add("input", {"target": {"tag": "input", "label": "PO number"}, "value": po, "page": page("/demo-erp#new", "New invoice")})
    add("input", {"target": {"tag": "input", "label": "Amount"}, "value": amount, "page": page("/demo-erp#new", "New invoice")})
    add("think", {"text": "Amount matches the PO within tolerance and receiving confirmed the delivery, so I code it to 6100 office expense and route for approval."})
    add("select", {"target": {"tag": "select", "label": "GL account"}, "value": "6100 Office expense", "page": page("/demo-erp#new", "New invoice")})
    add("input", {"target": {"tag": "input", "label": "Bank account", "name": "bank"}, "value": "", "masked": True, "page": page("/demo-erp#new", "New invoice")})
    add("input", {"target": {"tag": "textarea", "label": "Memo"}, "value": f"Per {po}; contact ap@vendor-real.com or 415-555-0199", "page": page("/demo-erp#new", "New invoice")})
    add("think", {"text": "Memo has the vendor contact; that gets redacted automatically. Submitting for approval."})
    add("submit", {"target": {"tag": "form", "label": "Save and route for approval"}, "page": page("/demo-erp#new", "New invoice")})
    return ev


def run_capture_episode(user: dict, base_url: str, variant: int, goal: str, workflow: str = "ap_invoice_processing",
                        outcome: str = "success") -> int:
    sid = enterprise.start_capture(user, goal, workflow)
    s = db.q1("SELECT * FROM sessions WHERE id=?", sid)
    enterprise.ingest(s, user, scripted_capture_events(base_url, variant))
    enterprise.stop_capture(s)
    enterprise.edit_capture(db.q1("SELECT * FROM sessions WHERE id=?", sid), user, outcome=outcome)
    return sid


def seed_full(base_url: str = "http://localhost:8000") -> dict:
    users = seed_base()
    dana, luis = users["dana@experts.example"], users["luis@experts.example"]
    admin = users["admin@seatrail.example"]
    out = {"sandbox_records": [], "capture_records": []}
    task_rows = db.q("SELECT * FROM tasks WHERE purpose='work' ORDER BY id")
    for i, t in enumerate(task_rows[:9]):
        expert = dana if i % 2 == 0 else luis
        if db.q1("SELECT id FROM sessions WHERE task_id=? AND user_id=?", t["id"], expert["id"]):
            continue
        sid = run_sandbox_episode(expert["id"], t["id"], seed=i)
        rid = quality.submit(sid)
        out["sandbox_records"].append(rid)
        if i < 7:
            quality.review(rid, admin["id"], True, "Looks good.")
    if out["sandbox_records"] and not db.q1("SELECT id FROM datasets"):
        market.create_dataset(admin["id"], "US SMB Accounting Agent Trajectories v1",
                              "Verified expert trajectories for bank reconciliation, AP three-way match and T&E "
                              "audit, with step-level reasoning and RL-ready environments.", 250000)
    # Enterprise
    sarah = users["sarah@halcyon.example"]
    steward = users["tom@halcyon.example"]
    enterprise.give_consent(sarah)
    sarah = db.q1("SELECT * FROM users WHERE id=?", sarah["id"])
    for v in range(3):
        sid = run_capture_episode(sarah, base_url, v, "Enter a vendor invoice against its PO and route it for approval")
        rid = quality.submit(sid)
        out["capture_records"].append(rid)
        if v < 2:
            quality.review(rid, steward["id"], True, "Approved for training.")
    # One unsubmitted capture so the employee review screen has something to show.
    run_capture_episode(sarah, base_url, 7, "", workflow="", outcome="")
    # Demo-only baseline runs from a scripted agent (clearly labeled), so the gap screens have content.
    if not db.q1("SELECT id FROM baselines"):
        for i, t in enumerate(task_rows[:6]):
            seed_demo_baseline(t, accuracy=1.0 if i % 3 == 0 else 0.55, seed=i)
    # A reviewer fix on one approved record, so preference export has an example.
    rec = db.q1("SELECT id, record FROM records WHERE source='sandbox' AND status='approved' ORDER BY id")
    if rec and not db.q1("SELECT id FROM step_corrections"):
        step = next((s for s in db.loads(rec["record"])["steps"] if s["action"] and s["action"]["tool"] == "edit_cell"), None)
        if step:
            quality.add_correction(rec["id"], admin, step["i"], "weak_reasoning",
                                   "Cite the evidence: amounts agree to the cent and dates are within the clearing "
                                   "window, so this item is cleared.", note="demo correction")
    buyer = users["data@frontier-lab.example"]
    if not db.q1("SELECT id FROM data_requests"):
        market.create_request(buyer, "bank_reconciliation", 40, 0.85, 1200000,
                              "More NSF returns and bank-fee edge cases, US GAAP.")
    return out


DEMO_BASELINE_MODEL = "demo-scripted-baseline"


def seed_demo_baseline(task_row: dict, accuracy: float, seed: int) -> None:
    """Fill the baselines table with a scripted run. Not a real model; labeled as such."""
    from . import config, sandbox
    from .verifier import verify
    state = db.loads(task_row["initial_state"])
    events = [e for e in scripted_sandbox_events(task_row, accuracy=accuracy, seed=seed) if e["type"] != "think"]
    kept = []
    for e in events:
        try:
            state = sandbox.apply(state, e["type"], e["payload"])
            kept.append(e)
        except sandbox.ActionError:
            pass
    v = verify(state, db.loads(task_row["answer_key"]), config.VERIFY_PASS_SCORE)
    db.x("INSERT INTO baselines (task_id, model, score, passed, steps, transcript, created_at) VALUES (?,?,?,?,?,?,?)",
         task_row["id"], DEMO_BASELINE_MODEL, v["score"], int(v["passed"]), len(kept),
         db.dumps({"stop": "submitted", "events": kept, "checks": v["checks"], "demo": True}), db.now())
