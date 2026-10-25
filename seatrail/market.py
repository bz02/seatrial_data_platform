"""Marketplace edition: curated datasets of approved sandbox records, sold to model builders.

Phase 1 (default): the platform is the only seller. Experts are paid a task reward on approval and
share EXPERT_REVENUE_SHARE of every sale of a dataset that contains their records.
Phase 2 (OPEN_MARKETPLACE=true): third-party sellers list datasets; the platform keeps TAKE_RATE.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

from . import config, db
from .enterprise import with_corrections, write_files

PLATFORM_ORG = 0


class MarketError(ValueError):
    pass


def create_dataset(admin_id: int, name: str, description: str, price_cents: int, template: str | None = None,
                   min_quality: float = 0.0, license_: str = "Commercial training license, no redistribution",
                   list_now: bool = True) -> dict:
    # Records made for a buyer's custom request are exclusive to that buyer and never go in the catalog.
    sql = ("SELECT id FROM records WHERE org_id=? AND source='sandbox' AND status='approved' AND quality_score>=?"
           " AND task_id NOT IN (SELECT id FROM tasks WHERE request_id IS NOT NULL)")
    args: list = [PLATFORM_ORG, min_quality]
    if template:
        sql += " AND template=?"
        args.append(template)
    ids = [r["id"] for r in db.q(sql, *args)]
    if not ids:
        raise MarketError("no approved records match these filters")
    with db.tx() as c:
        cur = c.execute("INSERT INTO datasets (seller_id, name, domain, description, price_cents, license, status,"
                        " created_at) VALUES (?,?,?,?,?,?,?,?)",
                        (config.PLATFORM_SELLER_ID, name, "accounting", description, price_cents, license_,
                         "listed" if list_now else "draft", db.now()))
        did = cur.lastrowid
        c.executemany("INSERT INTO dataset_items (dataset_id, record_id) VALUES (?,?)", [(did, i) for i in ids])
    db.audit(PLATFORM_ORG, admin_id, "dataset.create", f"dataset:{did}", records=len(ids))
    return get_dataset(did)


def create_seller_listing(seller: dict, name: str, description: str, price_cents: int, jsonl: str) -> dict:
    """Phase 2: a third-party seller lists their own dataset file. Disabled until the market opens."""
    if not config.OPEN_MARKETPLACE:
        raise PermissionError("open marketplace is not enabled")
    lines = [json.loads(l) for l in jsonl.splitlines() if l.strip()]
    if not lines or any(l.get("schema") != "seatrail.trajectory.v1" for l in lines):
        raise MarketError("upload must be JSONL of seatrail.trajectory.v1 records")
    did = db.x("INSERT INTO datasets (seller_id, name, domain, description, price_cents, license, status, created_at)"
               " VALUES (?,?,?,?,?,?, 'draft', ?)", seller["id"], name, lines[0].get("domain", "other"),
               description, price_cents, "Seller license", db.now())
    path = config.DATA_DIR / "seller_uploads" / f"dataset_{did}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(jsonl)
    return get_dataset(did)


def _stats(did: int) -> dict:
    rows = db.q("SELECT r.template, r.quality_score, r.record, r.user_id FROM records r JOIN dataset_items i"
                " ON i.record_id=r.id WHERE i.dataset_id=?", did)
    if not rows:
        return {"records": 0}
    steps = [len(db.loads(r["record"])["steps"]) for r in rows]
    by_t: dict[str, int] = {}
    for r in rows:
        by_t[r["template"]] = by_t.get(r["template"], 0) + 1
    return {"records": len(rows), "experts": len({r["user_id"] for r in rows}),
            "mean_quality": round(sum(r["quality_score"] for r in rows) / len(rows), 3),
            "mean_steps": round(sum(steps) / len(steps), 1), "templates": by_t, "verified": True}


def get_dataset(did: int) -> dict:
    d = db.q1("SELECT * FROM datasets WHERE id=?", did)
    if not d:
        raise KeyError("dataset not found")
    d["stats"] = _stats(did)
    d["seller"] = "SeaTrail (platform)" if d["seller_id"] == config.PLATFORM_SELLER_ID else \
        (db.q1("SELECT name FROM users WHERE id=?", d["seller_id"]) or {}).get("name")
    return d


def list_datasets(include_unlisted: bool = False) -> list[dict]:
    sql = "SELECT id FROM datasets" + ("" if include_unlisted else " WHERE status='listed'") + " ORDER BY id DESC"
    return [get_dataset(r["id"]) for r in db.q(sql)]


def sample(did: int) -> dict:
    """One record, trimmed, so buyers can inspect the format before paying."""
    r = db.q1("SELECT r.record FROM records r JOIN dataset_items i ON i.record_id=r.id WHERE i.dataset_id=?"
              " ORDER BY r.quality_score DESC LIMIT 1", did)
    if not r:
        return {}
    rec = db.loads(r["record"])
    rec["steps"] = [s for s in rec["steps"] if s["action"] and s["action"]["tool"] != "view"][:8] + \
        [{"note": f"... {len(rec['steps'])} steps in full record"}]
    rec["environment"] = {k: v for k, v in rec["environment"].items() if k != "initial_state"}
    rec["outcome"] = {"label": rec["outcome"]["label"], "verification_score": rec["outcome"]["verification"]["score"]}
    return rec


def settle(gross_cents: int, seller_id: int) -> dict:
    if seller_id == config.PLATFORM_SELLER_ID:
        pool = round(gross_cents * config.EXPERT_REVENUE_SHARE)
        return {"take_rate": 1.0, "platform_fee_cents": gross_cents - pool, "seller_net_cents": 0,
                "expert_pool_cents": pool}
    fee = round(gross_cents * config.TAKE_RATE)
    return {"take_rate": config.TAKE_RATE, "platform_fee_cents": fee, "seller_net_cents": gross_cents - fee,
            "expert_pool_cents": 0}


def purchase(did: int, buyer: dict, payment_ref: str | None = None) -> dict:
    d = get_dataset(did)
    if d["status"] != "listed":
        raise MarketError("dataset is not for sale")
    if db.q1("SELECT id FROM orders WHERE dataset_id=? AND buyer_id=?", did, buyer["id"]):
        raise MarketError("already purchased")
    s = settle(d["price_cents"], d["seller_id"])
    # Payment is mocked in the MVP. In production this is a Stripe PaymentIntent / invoice id.
    ref = payment_ref or f"mock_{secrets.token_hex(6)}"
    with db.tx() as c:
        cur = c.execute("INSERT INTO orders (dataset_id, buyer_id, seller_id, gross_cents, take_rate,"
                        " platform_fee_cents, seller_net_cents, expert_pool_cents, payment_ref, created_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (did, buyer["id"], d["seller_id"], d["price_cents"], s["take_rate"], s["platform_fee_cents"],
                         s["seller_net_cents"], s["expert_pool_cents"], ref, db.now()))
        oid = cur.lastrowid
        if s["expert_pool_cents"]:
            counts = c.execute("SELECT r.user_id, COUNT(*) FROM records r JOIN dataset_items i ON i.record_id=r.id"
                               " WHERE i.dataset_id=? GROUP BY r.user_id ORDER BY r.user_id", (did,)).fetchall()
            total = sum(n for _, n in counts)
            paid = 0
            for k, (uid, n) in enumerate(counts):
                amt = s["expert_pool_cents"] - paid if k == len(counts) - 1 else s["expert_pool_cents"] * n // total
                paid += amt
                c.execute("INSERT INTO payouts (expert_id, kind, amount_cents, order_id, created_at)"
                          " VALUES (?, 'revenue_share', ?, ?, ?)", (uid, amt, oid, db.now()))
    db.audit(PLATFORM_ORG, buyer["id"], "order.create", f"order:{oid}", gross=d["price_cents"])
    return db.q1("SELECT * FROM orders WHERE id=?", oid)


def build_download(did: int, buyer: dict) -> Path:
    if not db.q1("SELECT id FROM orders WHERE dataset_id=? AND buyer_id=?", did, buyer["id"]):
        raise PermissionError("purchase required")
    rows = db.q("SELECT r.id, r.record, r.quality_score FROM records r JOIN dataset_items i ON i.record_id=r.id"
                " WHERE i.dataset_id=? ORDER BY r.id", did)
    records = [with_corrections(r["id"], db.loads(r["record"])) for r in rows]
    out = config.DATA_DIR / "deliveries" / f"buyer_{buyer['id']}" / f"dataset_{did}"
    out.mkdir(parents=True, exist_ok=True)
    write_files(out, records, "both", title=get_dataset(did)["name"])
    db.audit(PLATFORM_ORG, buyer["id"], "dataset.download", f"dataset:{did}")
    return out


def earnings(expert_id: int) -> dict:
    rows = db.q("SELECT kind, amount_cents, record_id, order_id, created_at FROM payouts WHERE expert_id=?"
                " ORDER BY id DESC", expert_id)
    return {"total_cents": sum(r["amount_cents"] for r in rows),
            "task_rewards_cents": sum(r["amount_cents"] for r in rows if r["kind"] == "task_reward"),
            "revenue_share_cents": sum(r["amount_cents"] for r in rows if r["kind"] == "revenue_share"),
            "payouts": rows}


def ledger() -> dict:
    o = db.q1("SELECT COUNT(*) n, COALESCE(SUM(gross_cents),0) gross, COALESCE(SUM(platform_fee_cents),0) fee,"
              " COALESCE(SUM(seller_net_cents),0) seller, COALESCE(SUM(expert_pool_cents),0) pool FROM orders")
    rewards = db.q1("SELECT COALESCE(SUM(amount_cents),0) s FROM payouts WHERE kind='task_reward'")["s"]
    return {"orders": o["n"], "gross_cents": o["gross"], "platform_fee_cents": o["fee"],
            "seller_net_cents": o["seller"], "expert_revenue_share_cents": o["pool"],
            "task_rewards_paid_cents": rewards, "take_rate_open_market": config.TAKE_RATE,
            "expert_revenue_share": config.EXPERT_REVENUE_SHARE, "open_marketplace": config.OPEN_MARKETPLACE}


# ---------------------------------------------------------------------------
# Custom data requests: a buyer orders N verified trajectories for a task family; we generate
# fresh tasks for it (never seen by anyone else), experts complete them, and the approved records
# are delivered as a private dataset.
# ---------------------------------------------------------------------------
REQUEST_SEED_BASE = 100000


def create_request(buyer: dict, template: str, quantity: int, min_quality: float, budget_cents: int,
                   notes: str = "") -> dict:
    from .tasks import TEMPLATES
    if template not in TEMPLATES:
        raise MarketError(f"unknown task family {template!r}")
    if not 1 <= quantity <= 1000:
        raise MarketError("quantity must be between 1 and 1000")
    if budget_cents <= 0:
        raise MarketError("budget must be positive")
    rid = db.x("INSERT INTO data_requests (buyer_id, template, quantity, min_quality, budget_cents, notes, status,"
               " created_at) VALUES (?,?,?,?,?,?, 'open', ?)", buyer["id"], template, quantity,
               max(0.0, min(1.0, float(min_quality))), budget_cents, notes[:2000], db.now())
    db.audit(PLATFORM_ORG, buyer["id"], "request.create", f"request:{rid}", template=template, quantity=quantity)
    return get_request(rid)


def get_request(rid: int) -> dict:
    r = db.q1("SELECT * FROM data_requests WHERE id=?", rid)
    if not r:
        raise KeyError("request not found")
    tasks = db.q("SELECT id FROM tasks WHERE request_id=?", rid)
    approved = db.q("SELECT r.id FROM records r JOIN tasks t ON t.id=r.task_id WHERE t.request_id=? AND"
                    " r.status='approved' AND r.quality_score>=?", rid, r["min_quality"])
    r["progress"] = {"tasks": len(tasks), "approved_records": len(approved)}
    return r


def list_requests(buyer_id: int | None = None) -> list[dict]:
    sql = "SELECT id FROM data_requests" + (" WHERE buyer_id=?" if buyer_id is not None else "") + " ORDER BY id DESC"
    return [get_request(r["id"]) for r in db.q(sql, *([buyer_id] if buyer_id is not None else []))]


def produce_request(admin_id: int, rid: int) -> dict:
    from . import tasks
    r = get_request(rid)
    if r["status"] not in ("open", "in_production"):
        raise MarketError(f"request is {r['status']}")
    have = r["progress"]["tasks"]
    per_task = max(1, int(r["budget_cents"] * 0.6) // r["quantity"])  # about 60% of the budget goes to experts
    for k in range(have, r["quantity"]):
        tid = tasks.create_task(r["template"], REQUEST_SEED_BASE + rid * 1000 + k)
        db.x("UPDATE tasks SET request_id=?, reward_cents=? WHERE id=?", rid, int(per_task), tid)
    db.x("UPDATE data_requests SET status='in_production' WHERE id=?", rid)
    db.audit(PLATFORM_ORG, admin_id, "request.produce", f"request:{rid}")
    return get_request(rid)


def deliver_request(admin_id: int, rid: int) -> dict:
    r = get_request(rid)
    if r["status"] != "in_production":
        raise MarketError("request is not in production")
    ids = [x["id"] for x in db.q("SELECT r.id FROM records r JOIN tasks t ON t.id=r.task_id WHERE t.request_id=?"
                                 " AND r.status='approved' AND r.quality_score>=?", rid, r["min_quality"])]
    if not ids:
        raise MarketError("no approved records for this request yet")
    buyer = db.q1("SELECT * FROM users WHERE id=?", r["buyer_id"])
    with db.tx() as c:
        cur = c.execute("INSERT INTO datasets (seller_id, name, domain, description, price_cents, license, status,"
                        " created_at) VALUES (?,?,?,?,?,?, 'unlisted', ?)",
                        (config.PLATFORM_SELLER_ID, f"Custom request #{rid}: {r['template']}", "accounting",
                         f"{len(ids)} verified trajectories made for {buyer['name']}. Exclusive to this buyer.",
                         r["budget_cents"], "Exclusive commercial training license", db.now()))
        did = cur.lastrowid
        c.executemany("INSERT INTO dataset_items (dataset_id, record_id) VALUES (?,?)", [(did, i) for i in ids])
        c.execute("UPDATE data_requests SET status='delivered', dataset_id=? WHERE id=?", (did, rid))
    # Sell the private dataset to the requesting buyer (temporarily list it so purchase() accepts it).
    db.x("UPDATE datasets SET status='listed' WHERE id=?", did)
    try:
        purchase(did, buyer)
    finally:
        db.x("UPDATE datasets SET status='unlisted' WHERE id=?", did)
    db.audit(PLATFORM_ORG, admin_id, "request.deliver", f"request:{rid}", dataset=did, records=len(ids))
    return get_request(rid)
