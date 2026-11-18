"""Unit economics: expert earnings and payouts, revenue on acceptance, the warranty, batch P&L.

Earnings (booked automatically from task transitions):
    SME_REVIEW -> QA_APPROVED   author earns per_task_rate: (1 - HOLDBACK) payable now, HOLDBACK held
                                until the client accepts. Reviewer earns REVIEW_SHARE * their rate.
    SME_REVIEW -> REWORK        reviewer still earns the review fee, booked as REWORK cost (waste).
    DELIVERED  -> ACCEPTED      revenue = batch.price_per_accepted_task_usd; author holdback released.
Fixing your own task after QA or a warranty return is unpaid: an author is paid once per task.

Warranty: within batch.warranty_days of delivery a client can claim a defect. A valid claim returns
the task to REWORK and is resolved by a replacement (cost booked as WARRANTY_REPLACEMENT) or a
credit (negative revenue). Author-attributable defects (everything except TOO_EASY, which is model
drift and our risk, see P8) forfeit a held holdback or claw back a released one.
"""
from __future__ import annotations

import csv
import io
import os
from collections import Counter

from ... import db
from .. import store
from ..models import LedgerKind, TaskStatus

S = TaskStatus
HOLDBACK = float(os.environ.get("SEATRAIL_AUTHOR_HOLDBACK", "0.2"))
REVIEW_SHARE = float(os.environ.get("SEATRAIL_REVIEW_SHARE", "0.3"))
ACCEPTANCE_DAYS = float(os.environ.get("SEATRAIL_ACCEPTANCE_DAYS", "14"))
DEFAULT_CLAIM_RATE = float(os.environ.get("SEATRAIL_WARRANTY_RESERVE_RATE", "0.05"))
AUTHOR_FAULT = {"LEAKAGE", "BAD_VERIFIER", "AMBIGUOUS_PROMPT", "UNSOLVABLE"}


def _rate(user_id: int | None) -> float:
    if user_id is None:
        return 0.0
    r = db.q1("SELECT per_task_rate_usd FROM expert_profiles WHERE user_id=?", user_id)
    return float(r["per_task_rate_usd"]) if r else 0.0


def _line(user_id, task_id, role, kind, amount, status, note="") -> None:
    if amount:
        db.x("INSERT INTO payout_lines (user_id, task_id, role, kind, amount_usd, status, note, created_at, released_at)"
             " VALUES (?,?,?,?,?,?,?,?,?)", user_id, task_id, role, kind, round(amount, 2), status, note, db.now(),
             db.now() if status == "PAYABLE" else None)


# ---------------------------------------------------------------------------
# Transition hook
# ---------------------------------------------------------------------------
def on_transition(tid: int, frm: TaskStatus, to: TaskStatus, actor_id: int | None) -> None:
    t = db.q1("SELECT author_id, batch_id FROM foundry_tasks WHERE id=?", tid)
    if frm == S.SME_REVIEW and to in (S.QA_APPROVED, S.REWORK):
        rev = db.q1("SELECT reviewer_id FROM sme_assignments WHERE task_id=? AND active=1 ORDER BY id DESC LIMIT 1", tid)
        if rev:
            fee = round(_rate(rev["reviewer_id"]) * REVIEW_SHARE, 2)
            _line(rev["reviewer_id"], tid, "REVIEWER", "REVIEW", fee, "PAYABLE", f"QA review -> {to}")
            if fee:
                store.ledger(LedgerKind.SME_LABOR if to == S.QA_APPROVED else LedgerKind.REWORK, fee, tid,
                             f"review fee user {rev['reviewer_id']}")
    if frm == S.SME_REVIEW and to == S.QA_APPROVED and t["author_id"] is not None:
        paid = db.q1("SELECT 1 FROM payout_lines WHERE task_id=? AND role='AUTHOR' AND kind='BASE'", tid)
        rate = _rate(t["author_id"])
        if not paid and rate:
            _line(t["author_id"], tid, "AUTHOR", "BASE", rate * (1 - HOLDBACK), "PAYABLE", "QA approved")
            _line(t["author_id"], tid, "AUTHOR", "HOLDBACK", rate * HOLDBACK, "HELD", "released on client acceptance")
            store.ledger(LedgerKind.SME_LABOR, rate, tid, f"author fee user {t['author_id']}")
    if to == S.ACCEPTED:
        b = store.get_batch(t["batch_id"])
        if b["price_per_accepted_task_usd"]:
            db.x("INSERT INTO revenue_events (task_id, batch_id, kind, amount_usd, ref, created_at) VALUES"
                 " (?,?,?,?,?,?)", tid, t["batch_id"], "ACCEPTANCE", b["price_per_accepted_task_usd"],
                 "client acceptance", db.now())
        db.x("UPDATE payout_lines SET status='PAYABLE', released_at=? WHERE task_id=? AND kind='HOLDBACK'"
             " AND status='HELD'", db.now(), tid)


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------
def accept(task_ids: list[int], actor_id: int | None = None, reason: str = "client accepted") -> list[int]:
    done = []
    for tid in task_ids:
        if store.get_task(tid)["status"] == S.DELIVERED:
            store.transition(tid, S.ACCEPTED, actor_id, reason)
            done.append(tid)
    return done


def auto_accept(now: float | None = None) -> list[int]:
    """Deemed acceptance: delivered tasks with no open claim after ACCEPTANCE_DAYS."""
    now = now or db.now()
    due = []
    for r in db.q("SELECT id FROM foundry_tasks WHERE status='DELIVERED'"):
        d = store.delivered_at(r["id"])
        open_claim = db.q1("SELECT 1 FROM warranty_claims WHERE task_id=? AND status='OPEN'", r["id"])
        if d is not None and now - d >= ACCEPTANCE_DAYS * store.DAY and not open_claim:
            due.append(r["id"])
    return accept(due, None, f"deemed accepted after {ACCEPTANCE_DAYS:g} days")


# ---------------------------------------------------------------------------
# Warranty
# ---------------------------------------------------------------------------
def file_claim(task_id: int, defect_tag: str, detail: str) -> dict:
    t = store.get_task(task_id)
    if t["status"] not in (S.DELIVERED, S.ACCEPTED):
        raise store.sm.TransitionError(f"task is {t['status']}; only delivered work is under warranty", "not_delivered")
    if not store.warranty_open(task_id):
        raise store.sm.TransitionError("the warranty window for this task has closed", "warranty_expired")
    if len(detail.strip()) < 10:
        raise ValueError("describe the defect (10+ characters)")
    if db.q1("SELECT 1 FROM warranty_claims WHERE task_id=? AND status='OPEN'", task_id):
        raise store.sm.TransitionError("this task already has an open claim", "claim_open")
    cid = db.x("INSERT INTO warranty_claims (task_id, defect_tag, detail, created_at) VALUES (?,?,?,?)", task_id,
               defect_tag, detail.strip(), db.now())
    store.audit(task_id, "warranty_claim", None, claim_id=cid, defect_tag=defect_tag)
    return get_claim(cid)


def get_claim(cid: int) -> dict:
    c = db.q1("SELECT * FROM warranty_claims WHERE id=?", cid)
    if not c:
        raise KeyError(f"claim {cid} not found")
    return c


def claims(status: str | None = None) -> list[dict]:
    if status:
        return db.q("SELECT * FROM warranty_claims WHERE status=? ORDER BY id", status)
    return db.q("SELECT * FROM warranty_claims ORDER BY id")


def decide_claim(cid: int, actor_id: int, valid: bool, resolution: str = "REPLACED", note: str = "") -> dict:
    c = get_claim(cid)
    if c["status"] != "OPEN":
        raise store.sm.TransitionError(f"claim is already {c['status']}", "claim_decided")
    tid = c["task_id"]
    if not valid:
        db.x("UPDATE warranty_claims SET status='REJECTED', decision_note=?, decided_by=?, decided_at=? WHERE id=?",
             note, actor_id, db.now(), cid)
        store.audit(tid, "warranty_rejected", actor_id, claim_id=cid)
        return get_claim(cid)
    if resolution not in ("REPLACED", "CREDITED"):
        raise ValueError("resolution must be REPLACED or CREDITED")
    t = store.get_task(tid)
    store.transition(tid, S.RETURNED, actor_id, f"warranty claim {cid}: {c['defect_tag']}")
    store.transition(tid, S.REWORK, actor_id, f"warranty [{c['defect_tag']}]: {c['detail']}")
    b = store.get_batch(t["batch_id"])
    if resolution == "CREDITED":
        recognized = db.q1("SELECT COALESCE(SUM(amount_usd),0) s FROM revenue_events WHERE task_id=?", tid)["s"]
        credit = recognized if recognized > 0 else 0.0
        if credit:
            db.x("INSERT INTO revenue_events (task_id, batch_id, kind, amount_usd, ref, created_at) VALUES"
                 " (?,?,?,?,?,?)", tid, t["batch_id"], "CREDIT", -credit, f"warranty claim {cid}", db.now())
    else:
        made = db.q1("SELECT COALESCE(SUM(amount_usd),0) s FROM cost_ledger WHERE task_id=?", tid)["s"]
        store.ledger(LedgerKind.WARRANTY_REPLACEMENT, made, tid, f"warranty claim {cid}: replacement at cost")
    if c["defect_tag"] in AUTHOR_FAULT and t["author_id"] is not None:
        held = db.q("SELECT id FROM payout_lines WHERE task_id=? AND kind='HOLDBACK' AND status='HELD'", tid)
        db.x("UPDATE payout_lines SET status='FORFEITED', note=? WHERE task_id=? AND kind='HOLDBACK' AND status='HELD'",
             f"warranty claim {cid}", tid)
        if not held:
            released = db.q1("SELECT COALESCE(SUM(amount_usd),0) s FROM payout_lines WHERE task_id=? AND"
                             " kind='HOLDBACK' AND status IN ('PAYABLE','PAID')", tid)["s"]
            _line(t["author_id"], tid, "AUTHOR", "CLAWBACK", -released, "PAYABLE", f"warranty claim {cid}")
    db.x("UPDATE warranty_claims SET status='VALID', resolution=?, decision_note=?, decided_by=?, decided_at=?"
         " WHERE id=?", resolution, note, actor_id, db.now(), cid)
    store.audit(tid, "warranty_valid", actor_id, claim_id=cid, resolution=resolution, warranty_days=b["warranty_days"])
    return get_claim(cid)


def link_replacement(cid: int, replacement_task_id: int) -> dict:
    c = get_claim(cid)
    if c["status"] != "VALID" or c["resolution"] != "REPLACED":
        raise ValueError("only valid claims resolved by replacement take a replacement task")
    db.x("UPDATE warranty_claims SET replacement_task_id=? WHERE id=?", replacement_task_id, cid)
    db.x("UPDATE foundry_tasks SET parent_task_id=? WHERE id=? AND parent_task_id IS NULL", c["task_id"],
         replacement_task_id)
    return get_claim(cid)


# ---------------------------------------------------------------------------
# Payouts
# ---------------------------------------------------------------------------
def earnings(user_id: int) -> dict:
    rows = db.q("SELECT status, SUM(amount_usd) s FROM payout_lines WHERE user_id=? GROUP BY status", user_id)
    by = {r["status"]: round(r["s"], 2) for r in rows}
    lines = db.q("SELECT l.*, t.title FROM payout_lines l JOIN foundry_tasks t ON t.id=l.task_id WHERE l.user_id=?"
                 " ORDER BY l.id DESC LIMIT 200", user_id)
    return {"user_id": user_id, "by_status": by, "lines": lines,
            "payouts": db.q("SELECT * FROM expert_payouts WHERE user_id=? ORDER BY id DESC", user_id)}


def pending() -> list[dict]:
    return db.q("SELECT l.user_id, u.name, ROUND(SUM(l.amount_usd),2) amount_usd, COUNT(*) lines FROM payout_lines l"
                " JOIN users u ON u.id=l.user_id WHERE l.status='PAYABLE' GROUP BY l.user_id ORDER BY l.user_id")


def run_payouts(actor_id: int | None = None, reference: str = "") -> list[dict]:
    """Pay every expert whose payable balance is positive; a negative balance (clawback) carries over."""
    out = []
    ref = reference or f"payout-{int(db.now())}"
    for p in pending():
        if p["amount_usd"] <= 0:
            continue
        with db.tx() as c:
            cur = c.execute("INSERT INTO expert_payouts (user_id, amount_usd, line_count, reference, created_by, created_at)"
                            " VALUES (?,?,?,?,?,?)", (p["user_id"], p["amount_usd"], p["lines"], ref, actor_id, db.now()))
            c.execute("UPDATE payout_lines SET status='PAID', payout_id=? WHERE user_id=? AND status='PAYABLE'",
                      (cur.lastrowid, p["user_id"]))
        out.append(db.q1("SELECT * FROM expert_payouts WHERE id=?", cur.lastrowid))
    return out


def payout_csv(reference: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["payout_id", "user_id", "name", "email", "amount_usd", "lines", "reference"])
    for p in db.q("SELECT p.*, u.name, u.email FROM expert_payouts p JOIN users u ON u.id=p.user_id WHERE p.reference=?"
                  " ORDER BY p.id", reference):
        w.writerow([p["id"], p["user_id"], p["name"], p["email"], f"{p['amount_usd']:.2f}", p["line_count"], reference])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Unit economics
# ---------------------------------------------------------------------------
def _source_allocation(batch_id: int) -> float:
    """Each source's acquisition cost is spread evenly over every task built from it."""
    total = 0.0
    for a in db.q("SELECT DISTINCT s.id, s.acquisition_cost_usd c FROM source_assets s JOIN task_source_assets x"
                  " ON x.asset_id=s.id JOIN foundry_tasks t ON t.id=x.task_id WHERE t.batch_id=?", batch_id):
        if not a["c"]:
            continue
        users = db.q1("SELECT COUNT(*) n FROM task_source_assets WHERE asset_id=?", a["id"])["n"]
        mine = db.q1("SELECT COUNT(*) n FROM task_source_assets x JOIN foundry_tasks t ON t.id=x.task_id"
                     " WHERE x.asset_id=? AND t.batch_id=?", a["id"], batch_id)["n"]
        total += a["c"] * mine / users
    return round(total, 2)


def economics(batch_id: int) -> dict:
    b = store.get_batch(batch_id)
    tids = store.batch_task_ids(batch_id)
    status = Counter(store.get_task(t)["status"] for t in tids)
    ever = lambda st: db.q1("SELECT COUNT(DISTINCT x.task_id) n FROM task_transitions x JOIN foundry_tasks t ON"  # noqa: E731
                            " t.id=x.task_id WHERE t.batch_id=? AND x.to_status=?", batch_id, st)["n"]
    costs = {r["kind"]: round(r["s"], 2) for r in db.q("SELECT kind, SUM(amount_usd) s FROM cost_ledger WHERE"
                                                       " batch_id=? GROUP BY kind", batch_id)}
    src = _source_allocation(batch_id)
    if src:
        costs[LedgerKind.SOURCE_ACQUISITION] = round(costs.get(LedgerKind.SOURCE_ACQUISITION, 0) + src, 2)
    unattributed = round(db.q1("SELECT COALESCE(SUM(amount_usd),0) s FROM cost_ledger WHERE batch_id=? AND task_id"
                               " IS NULL", batch_id)["s"], 2)
    total_cost = round(sum(costs.values()), 2)
    revenue = round(db.q1("SELECT COALESCE(SUM(amount_usd),0) s FROM revenue_events WHERE batch_id=?",
                          batch_id)["s"], 2)
    accepted_now = status.get(S.ACCEPTED, 0)
    accepted_ever = ever(S.ACCEPTED)
    qa = db.q1("SELECT COUNT(*) n, SUM(decision='RETURN') r FROM qa_reviews q JOIN foundry_tasks t ON t.id=q.task_id"
               " WHERE t.batch_id=?", batch_id)
    delivered = ever(S.DELIVERED)
    valid_claims = db.q1("SELECT COUNT(*) n FROM warranty_claims c JOIN foundry_tasks t ON t.id=c.task_id WHERE"
                         " t.batch_id=? AND c.status='VALID'", batch_id)["n"]
    all_delivered = db.q1("SELECT COUNT(DISTINCT task_id) n FROM task_transitions WHERE to_status='DELIVERED'")["n"]
    all_valid = db.q1("SELECT COUNT(*) n FROM warranty_claims WHERE status='VALID'")["n"]
    claim_rate = max(DEFAULT_CLAIM_RATE, all_valid / all_delivered if all_delivered >= 20 else 0.0)
    at_risk = sum(r["s"] for r in db.q("SELECT task_id, SUM(amount_usd) s FROM revenue_events WHERE batch_id=?"
                                       " GROUP BY task_id", batch_id) if store.warranty_open(r["task_id"]))
    owed = db.q1("SELECT COALESCE(SUM(CASE WHEN l.status='PAYABLE' THEN l.amount_usd END),0) payable,"
                 " COALESCE(SUM(CASE WHEN l.status='HELD' THEN l.amount_usd END),0) held FROM payout_lines l JOIN"
                 " foundry_tasks t ON t.id=l.task_id WHERE t.batch_id=?", batch_id)
    cost_per_accepted = round(total_cost / accepted_now, 2) if accepted_now else None
    receivables = []
    for r in db.q("SELECT id, delivered_at FROM releases WHERE batch_id=? AND delivered_at IS NOT NULL", batch_id):
        amt = db.q1("SELECT COALESCE(SUM(e.amount_usd),0) s FROM revenue_events e JOIN release_tasks rt ON"
                    " rt.task_id=e.task_id WHERE rt.release_id=?", r["id"])["s"]
        receivables.append({"release_id": r["id"], "amount_usd": round(amt, 2),
                            "due_at": r["delivered_at"] + b["payment_terms_days"] * store.DAY})
    return {
        "batch_id": batch_id, "client_name": b["client_name"], "deal_type": b["deal_type"],
        "funnel": {"tasks": len(tids), "reached_sme_review": ever(S.SME_REVIEW), "qa_approved": ever(S.QA_APPROVED),
                   "delivered": delivered, "accepted": accepted_ever, "accepted_now": accepted_now,
                   "returned": ever(S.RETURNED), "status": dict(status)},
        "revenue_usd": revenue, "costs_usd": costs, "total_cost_usd": total_cost,
        "synthesis_waste_usd": unattributed,
        "gross_margin_usd": round(revenue - total_cost, 2),
        "gross_margin_pct": round((revenue - total_cost) / revenue, 4) if revenue else None,
        "cost_per_accepted_task_usd": cost_per_accepted,
        "quoted_cost_per_task_usd": b["quoted_cost_per_task_usd"],
        "over_quote": bool(cost_per_accepted and b["quoted_cost_per_task_usd"]
                           and cost_per_accepted > b["quoted_cost_per_task_usd"]),
        "price_per_accepted_task_usd": b["price_per_accepted_task_usd"],
        "qa_return_rate": round((qa["r"] or 0) / qa["n"], 4) if qa["n"] else None,
        "warranty": {"valid_claims": valid_claims, "claim_rate_used": round(claim_rate, 4),
                     "revenue_in_window_usd": round(at_risk, 2), "reserve_usd": round(at_risk * claim_rate, 2)},
        "liabilities": {"payouts_payable_usd": round(owed["payable"], 2), "payouts_held_usd": round(owed["held"], 2)},
        "receivables": receivables,
    }


def batches_overview() -> list[dict]:
    return [{"batch_id": b["id"], "client_name": b["client_name"], "deal_type": b["deal_type"],
             **{k: v for k, v in economics(b["id"]).items() if k in ("revenue_usd", "total_cost_usd",
                                                                    "gross_margin_pct", "cost_per_accepted_task_usd")},
             "priority": b["priority"]} for b in db.q("SELECT * FROM contract_batches ORDER BY id DESC")]
