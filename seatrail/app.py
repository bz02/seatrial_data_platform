"""HTTP API and static web apps.

Auth in the MVP is a demo stand-in: the browser sends X-User-Id. The browser recorder uses a
per-employee capture token issued when the employee consents. Replace both with SSO (OIDC/SAML)
before any real deployment.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, enterprise, market, sandbox
from .pipeline import quality
from .tasks import public_task

STATIC = Path(__file__).resolve().parent / "static"
RECORDER = Path(__file__).resolve().parent.parent / "recorder"

app = FastAPI(title="SeaTrail", version="0.1.0")
# The recorder posts from the company's own web apps, so the capture API accepts cross-origin calls.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(quality.SubmitError)
@app.exception_handler(market.MarketError)
@app.exception_handler(sandbox.ActionError)
@app.exception_handler(ValueError)
async def _bad_request(_: Request, e: Exception):
    return JSONResponse({"detail": str(e)}, status_code=400)


@app.exception_handler(PermissionError)
async def _forbidden(_: Request, e: Exception):
    return JSONResponse({"detail": str(e) or "forbidden"}, status_code=403)


@app.exception_handler(KeyError)
async def _not_found(_: Request, e: Exception):
    return JSONResponse({"detail": str(e).strip("'")}, status_code=404)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def current_user(x_user_id: int | None = Header(default=None)) -> dict:
    if x_user_id is None:
        raise HTTPException(401, "sign in required")
    u = db.q1("SELECT * FROM users WHERE id=?", x_user_id)
    if not u:
        raise HTTPException(401, "unknown user")
    return u


def need(user: dict, *roles: str) -> None:
    if user["role"] not in roles:
        raise HTTPException(403, f"requires role: {' or '.join(roles)}")


def capture_user(authorization: str | None = Header(default=None)) -> dict:
    token = (authorization or "").removeprefix("Bearer ").strip()
    u = enterprise.user_by_token(token)
    if not u:
        raise HTTPException(401, "invalid capture token")
    return u


def _session(sid: int) -> dict:
    s = db.q1("SELECT * FROM sessions WHERE id=?", sid)
    if not s:
        raise HTTPException(404, "session not found")
    return s


def _public_user(u: dict) -> dict:
    org = db.q1("SELECT name, kind FROM orgs WHERE id=?", u["org_id"])
    return {"id": u["id"], "name": u["name"], "role": u["role"], "email": u["email"], "org_id": u["org_id"],
            "org": org["name"], "org_kind": org["kind"], "credentials": u["credentials"],
            "consented": bool(u["consent_at"])}


@app.get("/api/me")
def me(user=Depends(current_user)):
    return _public_user(user)


@app.get("/api/demo/users")
def demo_users():
    return [_public_user(u) for u in db.q("SELECT * FROM users ORDER BY org_id, id")]


# ---------------------------------------------------------------------------
# Marketplace: expert sandbox tasks
# ---------------------------------------------------------------------------
@app.get("/api/tasks")
def list_tasks(user=Depends(current_user)):
    need(user, "expert")
    out = []
    for t in db.q("SELECT * FROM tasks WHERE active=1 ORDER BY id"):
        mine = db.q1("SELECT id, status FROM sessions WHERE task_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
                     t["id"], user["id"])
        out.append({**public_task(t), "my_session": mine})
    return out


@app.post("/api/tasks/{task_id}/start")
def start_task(task_id: int, user=Depends(current_user)):
    need(user, "expert")
    t = db.q1("SELECT * FROM tasks WHERE id=? AND active=1", task_id)
    if not t:
        raise HTTPException(404, "task not found")
    existing = db.q1("SELECT id FROM sessions WHERE task_id=? AND user_id=? AND status IN ('in_progress','checked')",
                     task_id, user["id"])
    if existing:
        return {"session_id": existing["id"], "resumed": True}
    if db.q1("SELECT id FROM sessions WHERE task_id=? AND user_id=? AND status='submitted'", task_id, user["id"]):
        raise HTTPException(400, "you already submitted this task")
    sid = db.x("INSERT INTO sessions (org_id, source, task_id, user_id, status, state, started_at)"
               " VALUES (?, 'sandbox', ?, ?, 'in_progress', ?, ?)", market.PLATFORM_ORG, task_id, user["id"],
               t["initial_state"], db.now())
    return {"session_id": sid, "resumed": False}


def _own_sandbox(sid: int, user: dict) -> dict:
    s = _session(sid)
    if s["user_id"] != user["id"] or s["source"] != "sandbox":
        raise HTTPException(403, "not your session")
    return s


@app.get("/api/sessions/{sid}")
def get_session(sid: int, user=Depends(current_user)):
    s = _own_sandbox(sid, user)
    t = db.q1("SELECT * FROM tasks WHERE id=?", s["task_id"])
    events = db.q("SELECT seq, ts, type, payload FROM events WHERE session_id=? ORDER BY seq", sid)
    return {"id": s["id"], "status": s["status"], "task": public_task(t), "state": db.loads(s["state"]),
            "events": [{**e, "payload": db.loads(e["payload"])} for e in events],
            "report": db.loads(s["last_report"]), "tools": sandbox.TOOLS}


@app.post("/api/sessions/{sid}/events")
def post_events(sid: int, body: dict = Body(...), user=Depends(current_user)):
    s = _own_sandbox(sid, user)
    if s["status"] == "submitted":
        raise HTTPException(400, "session already submitted")
    state = db.loads(s["state"])
    last = db.q1("SELECT MAX(seq) m FROM events WHERE session_id=?", sid)["m"] or 0
    errors, accepted = [], 0
    with db.tx() as c:
        for e in body.get("events", []):
            try:
                state = sandbox.apply(state, e["type"], e.get("payload") or {})
            except sandbox.ActionError as err:
                errors.append({"event": e, "error": str(err)})
                continue
            last += 1
            c.execute("INSERT INTO events (session_id, seq, ts, type, payload) VALUES (?,?,?,?,?)",
                      (sid, last, float(e.get("ts", 0)), e["type"], db.dumps(e.get("payload") or {})))
            accepted += 1
        c.execute("UPDATE sessions SET state=?, status=CASE WHEN status='checked' THEN 'in_progress' ELSE status END"
                  " WHERE id=?", (db.dumps(state), sid))
    return {"accepted": accepted, "errors": errors, "state": state}


@app.post("/api/sessions/{sid}/check")
def check_session(sid: int, user=Depends(current_user)):
    _own_sandbox(sid, user)
    return quality.assess(sid)


@app.post("/api/sessions/{sid}/submit")
def submit_session(sid: int, user=Depends(current_user)):
    _own_sandbox(sid, user)
    return {"record_id": quality.submit(sid)}


@app.get("/api/me/earnings")
def my_earnings(user=Depends(current_user)):
    need(user, "expert")
    out = market.earnings(user["id"])
    out["records"] = db.q("SELECT id, template, quality_score, status, created_at FROM records WHERE user_id=?"
                          " ORDER BY id DESC", user["id"])
    return out


# ---------------------------------------------------------------------------
# Review queue (platform admin for marketplace; data steward inside an enterprise)
# ---------------------------------------------------------------------------
def _reviewer(user: dict) -> None:
    need(user, "admin", "steward", "org_admin")


@app.get("/api/review/queue")
def review_queue(status: str = "needs_review", user=Depends(current_user)):
    _reviewer(user)
    rows = db.q("SELECT r.id, r.source, r.template, r.quality_score, r.status, r.flags, r.created_at,"
                " u.name AS author FROM records r JOIN users u ON u.id=r.user_id WHERE r.org_id=? AND r.status=?"
                " ORDER BY r.quality_score DESC, r.id", user["org_id"], status)
    for r in rows:
        r["flags"] = db.loads(r["flags"])
    return rows


@app.get("/api/review/records/{rid}")
def review_record(rid: int, user=Depends(current_user)):
    _reviewer(user)
    r = db.q1("SELECT r.*, u.name AS author, u.credentials FROM records r JOIN users u ON u.id=r.user_id"
              " WHERE r.id=?", rid)
    if not r or r["org_id"] != user["org_id"]:
        raise HTTPException(404, "record not found")
    for k in ("record", "verification", "judge", "pii", "flags"):
        r[k] = db.loads(r[k])
    db.audit(user["org_id"], user["id"], "record.view", f"record:{rid}")
    return r


@app.post("/api/review/records/{rid}")
def review_decide(rid: int, body: dict = Body(...), user=Depends(current_user)):
    _reviewer(user)
    r = db.q1("SELECT org_id FROM records WHERE id=?", rid)
    if not r or r["org_id"] != user["org_id"]:
        raise HTTPException(404, "record not found")
    return quality.review(rid, user["id"], bool(body.get("approve")), body.get("notes", ""))


# ---------------------------------------------------------------------------
# Marketplace: datasets, orders, payouts
# ---------------------------------------------------------------------------
@app.get("/api/market/datasets")
def datasets():
    return market.list_datasets()


@app.get("/api/market/datasets/{did}")
def dataset(did: int, x_user_id: int | None = Header(default=None)):
    d = market.get_dataset(did)
    d["sample"] = market.sample(did)
    d["purchased"] = bool(x_user_id and db.q1("SELECT id FROM orders WHERE dataset_id=? AND buyer_id=?",
                                              did, x_user_id))
    return d


@app.post("/api/market/datasets/{did}/purchase")
def buy(did: int, user=Depends(current_user)):
    need(user, "buyer")
    return market.purchase(did, user)


@app.get("/api/market/purchases")
def purchases(user=Depends(current_user)):
    need(user, "buyer")
    return db.q("SELECT o.*, d.name FROM orders o JOIN datasets d ON d.id=o.dataset_id WHERE o.buyer_id=?"
                " ORDER BY o.id DESC", user["id"])


DOWNLOADABLE = {"sft_chat.jsonl", "trajectories.jsonl", "rl_environments.jsonl", "datacard.md", "manifest.json"}


@app.get("/api/market/datasets/{did}/download/{name}")
def download(did: int, name: str, user=Depends(current_user)):
    need(user, "buyer")
    if name not in DOWNLOADABLE:
        raise HTTPException(404, "no such file")
    path = market.build_download(did, user) / name
    if not path.exists():
        raise HTTPException(404, "no such file")
    return FileResponse(path, filename=f"dataset_{did}_{name}")


@app.post("/api/admin/datasets")
def admin_create_dataset(body: dict = Body(...), user=Depends(current_user)):
    need(user, "admin")
    return market.create_dataset(user["id"], body["name"], body.get("description", ""), int(body["price_cents"]),
                                 body.get("template") or None, float(body.get("min_quality", 0)))


@app.get("/api/admin/datasets")
def admin_datasets(user=Depends(current_user)):
    need(user, "admin")
    return market.list_datasets(include_unlisted=True)


@app.get("/api/admin/ledger")
def admin_ledger(user=Depends(current_user)):
    need(user, "admin")
    return market.ledger()


@app.post("/api/market/seller/listings")
def seller_listing(body: dict = Body(...), user=Depends(current_user)):
    need(user, "seller")
    return market.create_seller_listing(user, body["name"], body.get("description", ""), int(body["price_cents"]),
                                        body["jsonl"])


@app.get("/api/market/config")
def market_config():
    return {"open_marketplace": config.OPEN_MARKETPLACE, "take_rate": config.TAKE_RATE,
            "expert_revenue_share": config.EXPERT_REVENUE_SHARE}


# ---------------------------------------------------------------------------
# Enterprise edition
# ---------------------------------------------------------------------------
ENT_ROLES = ("employee", "steward", "ml_engineer", "org_admin")


def _ent(user: dict, *roles: str) -> None:
    need(user, *(roles or ENT_ROLES))


@app.get("/api/ent/policy")
def ent_policy(user=Depends(current_user)):
    _ent(user)
    return enterprise.get_policy(user["org_id"])


@app.put("/api/ent/policy")
def ent_update_policy(body: dict = Body(...), user=Depends(current_user)):
    _ent(user, "org_admin")
    return enterprise.update_policy(user["org_id"], user["id"], body)


@app.post("/api/ent/consent")
def ent_consent(user=Depends(current_user)):
    _ent(user, "employee")
    return {"capture_token": enterprise.give_consent(user)}


@app.delete("/api/ent/consent")
def ent_revoke(user=Depends(current_user)):
    _ent(user, "employee")
    enterprise.revoke_consent(user)
    return {"ok": True}


@app.get("/api/ent/my/token")
def ent_my_token(user=Depends(current_user)):
    _ent(user, "employee")
    return {"capture_token": user["capture_token"], "consented": bool(user["consent_at"])}


@app.get("/api/ent/my/sessions")
def ent_my_sessions(user=Depends(current_user)):
    _ent(user, "employee")
    rows = db.q("SELECT s.id, s.status, s.goal, s.outcome, s.state, s.started_at, s.ended_at,"
                " (SELECT COUNT(*) FROM events e WHERE e.session_id=s.id AND e.deleted=0) AS n_events,"
                " (SELECT r.status FROM records r WHERE r.session_id=s.id) AS record_status"
                " FROM sessions s WHERE s.user_id=? AND s.source='capture' AND s.status<>'discarded'"
                " ORDER BY s.id DESC", user["id"])
    for r in rows:
        r["workflow"] = (db.loads(r.pop("state")) or {}).get("workflow", "")
    return rows


def _own_capture(sid: int, user: dict) -> dict:
    s = _session(sid)
    if s["user_id"] != user["id"] or s["source"] != "capture":
        raise HTTPException(403, "not your session")
    return s


def _summarize(e: dict) -> str:
    p = e["payload"]
    t = p.get("target") or {}
    label = t.get("label") or t.get("text") or t.get("name") or t.get("tag") or ""
    if e["type"] == "navigate":
        return f"Opened {p.get('url', '')}"
    if e["type"] == "click":
        return f"Clicked {label}"
    if e["type"] == "input":
        return f"Typed into {label}: {'[masked]' if p.get('masked') else p.get('value', '')}"
    if e["type"] == "select":
        return f"Chose {p.get('value', '')} in {label}"
    if e["type"] == "key":
        return f"Pressed {p.get('key', '')}"
    if e["type"] == "submit":
        return f"Submitted {label or 'form'}"
    if e["type"] == "think":
        return f"Said: {p.get('text', '')}"
    return e["type"]


@app.get("/api/ent/sessions/{sid}")
def ent_session(sid: int, user=Depends(current_user)):
    _ent(user, "employee")
    s = _own_capture(sid, user)
    events = db.q("SELECT seq, ts, type, payload, deleted FROM events WHERE session_id=? ORDER BY seq", sid)
    for e in events:
        e["payload"] = db.loads(e["payload"])
        e["summary"] = "(deleted)" if e["deleted"] else _summarize(e)
        del e["payload"]
    record = None
    if events:
        record, _ = quality.build_record(sid)
    return {"id": s["id"], "status": s["status"], "goal": s["goal"], "outcome": s["outcome"],
            "workflow": (db.loads(s["state"]) or {}).get("workflow", ""), "events": events,
            "preview": record, "report": db.loads(s["last_report"]),
            "workflows": enterprise.get_policy(user["org_id"])["workflows"]}


@app.patch("/api/ent/sessions/{sid}")
def ent_edit(sid: int, body: dict = Body(...), user=Depends(current_user)):
    _ent(user, "employee")
    s = _own_capture(sid, user)
    enterprise.edit_capture(s, user, goal=body.get("goal"), workflow=body.get("workflow"),
                            outcome=body.get("outcome"), delete_seqs=body.get("delete_seqs"))
    return {"ok": True}


@app.post("/api/ent/sessions/{sid}/check")
def ent_check(sid: int, user=Depends(current_user)):
    _ent(user, "employee")
    _own_capture(sid, user)
    return quality.assess(sid)


@app.post("/api/ent/sessions/{sid}/submit")
def ent_submit(sid: int, user=Depends(current_user)):
    _ent(user, "employee")
    _own_capture(sid, user)
    return {"record_id": quality.submit(sid)}


@app.post("/api/ent/sessions/{sid}/discard")
def ent_discard(sid: int, user=Depends(current_user)):
    _ent(user, "employee")
    enterprise.discard_capture(_own_capture(sid, user), user)
    return {"ok": True}


@app.post("/api/ent/exports")
def ent_export(body: dict = Body(...), user=Depends(current_user)):
    _ent(user, "ml_engineer", "org_admin")
    return enterprise.create_export(user["org_id"], user["id"], body.get("format", "both"),
                                    body.get("workflow") or None, float(body.get("min_quality", 0)))


@app.get("/api/ent/exports")
def ent_exports(user=Depends(current_user)):
    _ent(user, "ml_engineer", "org_admin")
    rows = db.q("SELECT e.*, u.name AS requested_by_name FROM exports e JOIN users u ON u.id=e.requested_by"
                " WHERE e.org_id=? ORDER BY e.id DESC", user["org_id"])
    for r in rows:
        r["record_ids"] = db.loads(r["record_ids"])
        r["filters"] = db.loads(r["filters"])
        r["files"] = sorted(p.name for p in Path(r["path"]).iterdir()) if r["path"] and Path(r["path"]).exists() else []
    return rows


@app.get("/api/ent/exports/{eid}/{name}")
def ent_export_file(eid: int, name: str, user=Depends(current_user)):
    _ent(user, "ml_engineer", "org_admin")
    e = db.q1("SELECT * FROM exports WHERE id=? AND org_id=?", eid, user["org_id"])
    if not e or name not in DOWNLOADABLE or not (Path(e["path"]) / name).exists():
        raise HTTPException(404, "no such file")
    db.audit(user["org_id"], user["id"], "export.download", f"export:{eid}", file=name)
    return FileResponse(Path(e["path"]) / name, filename=f"export_{eid}_{name}")


@app.get("/api/ent/audit")
def ent_audit(user=Depends(current_user)):
    _ent(user, "org_admin")
    rows = db.q("SELECT a.*, u.name AS user_name FROM audit_log a LEFT JOIN users u ON u.id=a.user_id"
                " WHERE a.org_id=? ORDER BY a.id DESC LIMIT 200", user["org_id"])
    for r in rows:
        r["detail"] = db.loads(r["detail"])
    return rows


@app.post("/api/ent/retention/purge")
def ent_purge(user=Depends(current_user)):
    _ent(user, "org_admin")
    return enterprise.purge_expired(user["org_id"], user["id"])


@app.get("/api/ent/stats")
def ent_stats(user=Depends(current_user)):
    _ent(user, "steward", "ml_engineer", "org_admin")
    o = user["org_id"]
    by_status = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM records WHERE org_id=?"
                                                    " GROUP BY status", o)}
    by_wf = db.q("SELECT template AS workflow, COUNT(*) n, ROUND(AVG(quality_score),3) q FROM records"
                 " WHERE org_id=? AND status='approved' GROUP BY template ORDER BY n DESC", o)
    return {"employees": db.q1("SELECT COUNT(*) n FROM users WHERE org_id=? AND role='employee'", o)["n"],
            "consented": db.q1("SELECT COUNT(*) n FROM users WHERE org_id=? AND role='employee'"
                               " AND consent_at IS NOT NULL", o)["n"],
            "sessions": db.q1("SELECT COUNT(*) n FROM sessions WHERE org_id=? AND status<>'discarded'", o)["n"],
            "records": by_status, "approved_by_workflow": by_wf}


# ---------------------------------------------------------------------------
# Capture API, called by the browser recorder with a capture token
# ---------------------------------------------------------------------------
@app.get("/api/capture/config")
def cap_config(user=Depends(capture_user)):
    return enterprise.recorder_config(user)


@app.post("/api/capture/sessions")
def cap_start(body: dict = Body(default={}), user=Depends(capture_user)):
    return {"session_id": enterprise.start_capture(user, body.get("goal", ""), body.get("workflow", ""))}


@app.post("/api/capture/sessions/{sid}/events")
def cap_events(sid: int, body: dict = Body(...), user=Depends(capture_user)):
    return enterprise.ingest(_session(sid), user, body.get("events", []))


@app.post("/api/capture/sessions/{sid}/stop")
def cap_stop(sid: int, user=Depends(capture_user)):
    s = _session(sid)
    if s["user_id"] != user["id"]:
        raise HTTPException(403, "not your session")
    enterprise.stop_capture(s)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Web apps
# ---------------------------------------------------------------------------
PAGES = {"": "index.html", "expert": "expert.html", "review": "review.html", "market": "market.html",
         "enterprise": "enterprise.html", "demo-erp": "demo-erp.html"}


def _page(name):
    def handler():
        return FileResponse(STATIC / name)
    return handler


for route, file in PAGES.items():
    app.get("/" + route, include_in_schema=False)(_page(file))

app.mount("/static", StaticFiles(directory=STATIC), name="static")
if RECORDER.exists():
    app.mount("/recorder", StaticFiles(directory=RECORDER), name="recorder")


@app.get("/health")
def health():
    return {"ok": True, "judge": config.JUDGE_ENABLED, "open_marketplace": config.OPEN_MARKETPLACE}
