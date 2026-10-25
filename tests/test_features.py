"""Auth, expert qualification, consensus review, corrections, discovery, baselines, requests, leads."""
import json
from types import SimpleNamespace as NS

from conftest import as_user

from seatrail import config, db, demo
from seatrail.pipeline import agent

BASE = "http://localhost:8000"


def _work(client, expert, task_id, **kw):
    sid = client.post(f"/api/tasks/{task_id}/start", headers=as_user(expert)).json()["session_id"]
    t = db.q1("SELECT * FROM tasks WHERE id=?", task_id)
    client.post(f"/api/sessions/{sid}/events", json={"events": demo.scripted_sandbox_events(t, **kw)},
                headers=as_user(expert))
    return sid


# --- auth --------------------------------------------------------------------------------------
def test_signup_login_logout_and_csrf(client, monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", False)
    r = client.post("/api/auth/signup", json={"name": "Ana Park", "email": "ana@cpa.example",
                                              "password": "correct horse battery", "role": "expert"})
    assert r.status_code == 200 and r.json()["tier"] == "applicant"
    assert client.get("/api/me").json()["email"] == "ana@cpa.example"  # cookie session
    # Header impersonation is off outside demo mode
    client.post("/api/auth/logout")
    assert client.get("/api/me", headers={"X-User-Id": "1"}).status_code == 401
    # Can't self-register as admin, short passwords rejected, wrong password rejected
    assert client.post("/api/auth/signup", json={"name": "x", "email": "x@x.example", "password": "long enough pw",
                                                 "role": "admin"}).status_code == 400
    assert client.post("/api/auth/signup", json={"name": "y", "email": "y@y.example", "password": "short",
                                                 "role": "buyer"}).status_code == 400
    assert client.post("/api/auth/login", json={"email": "ana@cpa.example", "password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "ana@cpa.example",
                                                "password": "correct horse battery"}).status_code == 200
    # Mutating call without the CSRF header is refused
    from fastapi.testclient import TestClient
    from seatrail.app import app
    bare = TestClient(app)
    assert bare.post("/api/auth/logout").status_code == 403
    assert client.get("/api/demo/users").status_code == 404


def test_login_throttle(client):
    for _ in range(8):
        client.post("/api/auth/login", json={"email": "dana@experts.example", "password": "wrong"})
    r = client.post("/api/auth/login", json={"email": "dana@experts.example", "password": "wrong"})
    assert r.status_code == 403 and "too many" in r.json()["detail"]


# --- expert qualification ----------------------------------------------------------------------
def test_new_expert_must_qualify(client, monkeypatch):
    client.post("/api/auth/signup", json={"name": "Ana Park", "email": "ana@cpa.example",
                                          "password": "correct horse battery", "role": "expert"})
    ana = db.q1("SELECT * FROM users WHERE email='ana@cpa.example'")
    tasks = client.get("/api/tasks", headers=as_user(ana)).json()
    work = [t for t in tasks if t["purpose"] == "work"]
    qual = [t for t in tasks if t["purpose"] == "qualification"]
    assert all(t["locked"] for t in work) and not any(t["locked"] for t in qual)
    assert client.post(f"/api/tasks/{work[0]['id']}/start", headers=as_user(ana)).status_code == 403

    q = next(t for t in qual if t["template"] == work[0]["template"])
    # A sloppy attempt fails and does not unlock anything
    sid = _work(client, ana, q["id"], seed=3, accuracy=0.3)
    res = client.post(f"/api/sessions/{sid}/submit", headers=as_user(ana)).json()["qualification"]
    assert not res["passed"]
    assert client.get("/api/me/expert", headers=as_user(ana)).json()["tier"] == "applicant"
    assert db.q1("SELECT COUNT(*) n FROM records WHERE user_id=?", ana["id"])["n"] == 0  # never sold

    # A retake is served on a fresh variant, so the first attempt can't be memorized
    sid2 = client.post(f"/api/tasks/{q['id']}/start", headers=as_user(ana)).json()["session_id"]
    retake = db.q1("SELECT t.* FROM sessions s JOIN tasks t ON t.id=s.task_id WHERE s.id=?", sid2)
    assert retake["id"] != q["id"] and retake["purpose"] == "qualification"
    client.post(f"/api/sessions/{sid2}/events", headers=as_user(ana),
                json={"events": demo.scripted_sandbox_events(retake, seed=4)})
    sid = sid2
    assert client.post(f"/api/sessions/{sid}/submit", headers=as_user(ana)).json()["qualification"]["passed"]
    prof = client.get("/api/me/expert", headers=as_user(ana)).json()
    assert prof["tier"] == "qualified" and q["template"] in prof["qualified_templates"]
    assert client.post(f"/api/tasks/{work[0]['id']}/start", headers=as_user(ana)).status_code == 200
    other = next(t for t in work if t["template"] != q["template"])
    assert client.post(f"/api/tasks/{other['id']}/start", headers=as_user(ana)).status_code == 403


# --- consensus review + corrections + preference export -----------------------------------------
def test_consensus_and_corrections(client, env, monkeypatch):
    dana, admin = env["dana@experts.example"], env["admin@seatrail.example"]
    luis = env["luis@experts.example"]
    monkeypatch.setattr(config, "DOUBLE_REVIEW_BELOW", 1.01)  # force two reviews
    db.x("INSERT INTO users (org_id, role, name, email, created_at) VALUES (0,'admin','Second Admin',"
         "'admin2@seatrail.example', ?)", db.now())
    admin2 = db.q1("SELECT * FROM users WHERE email='admin2@seatrail.example'")
    t = db.q1("SELECT id FROM tasks WHERE purpose='work' ORDER BY id")
    sid = _work(client, dana, t["id"], seed=1)
    rid = client.post(f"/api/sessions/{sid}/submit", headers=as_user(dana)).json()["record_id"]

    rec = client.get(f"/api/review/records/{rid}", headers=as_user(admin)).json()
    assert rec["required_reviews"] == 2
    step = next(s for s in rec["record"]["steps"] if s["action"] and s["action"]["tool"] == "edit_cell")
    bad = client.post(f"/api/review/records/{rid}/corrections", headers=as_user(admin),
                      json={"step_i": step["i"], "issue": "weak_reasoning", "corrected_thought": ""})
    assert bad.status_code == 400
    ok = client.post(f"/api/review/records/{rid}/corrections", headers=as_user(admin), json={
        "step_i": step["i"], "issue": "weak_reasoning",
        "corrected_thought": "Bank and book amounts agree to the cent and the dates are two days apart, so cleared.",
        "note": "cite the evidence"})
    assert ok.status_code == 200 and len(ok.json()["corrections"]) == 1

    r1 = client.post(f"/api/review/records/{rid}", headers=as_user(admin), json={"approve": True}).json()
    assert r1["status"] == "needs_review" and r1["approvals"] == 1
    assert client.post(f"/api/review/records/{rid}", headers=as_user(admin), json={"approve": True}).status_code == 400
    r2 = client.post(f"/api/review/records/{rid}", headers=as_user(admin2), json={"approve": True}).json()
    assert r2["status"] == "approved"
    assert client.get("/api/review/stats", headers=as_user(admin)).json()["agreement"] == 1.0

    from seatrail import enterprise, market
    d = market.create_dataset(admin["id"], "t", "t", 1000)
    buyer = env["data@frontier-lab.example"]
    market.purchase(d["id"], buyer)
    out = market.build_download(d["id"], buyer)
    prefs = [json.loads(l) for l in (out / "preferences.jsonl").read_text().splitlines()]
    assert len(prefs) == 1 and "two days apart" in prefs[0]["chosen"][0]["content"]
    assert prefs[0]["rejected"][0]["content"] != prefs[0]["chosen"][0]["content"]
    sft = (out / "sft_chat.jsonl").read_text()
    assert "two days apart" in sft


# --- process discovery ---------------------------------------------------------------------------
def test_discovery(client, env):
    sarah, tom = env["sarah@halcyon.example"], env["tom@halcyon.example"]
    from seatrail import enterprise
    enterprise.give_consent(sarah)
    sarah = db.q1("SELECT * FROM users WHERE id=?", sarah["id"])
    for v in range(3):
        demo.run_capture_episode(sarah, BASE, v, "Enter a vendor invoice")
    s = client.get("/api/ent/discovery", headers=as_user(tom)).json()
    wf = s["workflows"][0]
    assert wf["workflow"] == "ap_invoice_processing" and wf["episodes"] == 3 and wf["collection_priority"] > 0
    m = client.get("/api/ent/discovery/ap_invoice_processing", headers=as_user(tom)).json()
    assert m["nodes"] and m["edges"] and m["variants"][0]["count"] >= 1
    assert not any("INV-" in n["id"] for n in m["nodes"])  # ids abstracted away
    assert client.get("/api/ent/discovery", headers=as_user(sarah)).status_code == 403


def test_enterprise_invite(client, env):
    priya, tom = env["priya@halcyon.example"], env["tom@halcyon.example"]
    r = client.post("/api/ent/users", headers=as_user(priya),
                    json={"name": "New Hire", "email": "new@halcyon.example", "role": "employee"}).json()
    assert r["user"]["org_id"] == 1 and len(r["temporary_password"]) >= 12
    assert client.post("/api/auth/login", json={"email": "new@halcyon.example",
                                                "password": r["temporary_password"]}).status_code == 200
    assert client.post("/api/ent/users", headers=as_user(tom), json={}).status_code == 403


# --- model baseline ------------------------------------------------------------------------------
class FakeClient:
    """Plays a fixed script of tool calls, like the model would."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        assert kw["model"] == config.BASELINE_MODEL and kw["thinking"] == {"type": "adaptive"}
        assert kw["fallbacks"] == "default" and all(t["input_schema"]["type"] == "object" for t in kw["tools"])
        self.calls += 1
        if not self.script:
            return NS(stop_reason="end_turn", content=[NS(type="text", text="Done.")])
        name, args = self.script.pop(0)
        return NS(stop_reason="tool_use", content=[NS(type="text", text=f"Doing {name}."),
                                                   NS(type="tool_use", id=f"t{self.calls}", name=name, input=args)])


def test_baseline_and_hard_task_bonus(client, env):
    admin, dana = env["admin@seatrail.example"], env["dana@experts.example"]
    t = db.q1("SELECT * FROM tasks WHERE purpose='work' ORDER BY id")
    # A weak model: looks around, makes one bad edit, submits.
    fake = FakeClient([("view", {"target": "brief"}), ("edit_cell", {"sheet": "nope", "row": "x", "column": "y",
                                                                     "value": "z"}), ("submit", {})])
    res = agent.run_baseline(t["id"], client=fake)
    assert res["passed"] is False and res["stop"] == "submitted" and res["steps"] == 3
    row = db.q1("SELECT transcript FROM baselines WHERE id=?", res["id"])
    assert "error" in json.dumps(db.loads(row["transcript"]))
    listed = next(x for x in client.get("/api/tasks", headers=as_user(dana)).json() if x["id"] == t["id"])
    assert listed["model_baseline"]["passed"] == 0
    assert listed["reward_cents"] == round(t["reward_cents"] * config.HARD_TASK_BONUS)
    gap = client.get("/api/admin/baselines", headers=as_user(admin)).json()["gap"]
    assert gap[0]["model_pass_rate"] == 0.0
    assert client.get("/api/admin/baselines", headers=as_user(dana)).status_code == 403


def test_observe_never_shows_answer_key(env):
    t = db.q1("SELECT * FROM tasks WHERE purpose='work' ORDER BY id")
    state = db.loads(t["initial_state"])
    key = json.loads(t["answer_key"])
    for target in ["brief", "fields", "inbox"] + [f"sheet:{s['name']}" for s in state["sheets"]]:
        out = agent.observe(state, t, target)
        assert "answer" not in out.lower() or "answer" in t["brief"].lower()
    assert "cells" not in agent._task_prompt(t, state) and key


# --- custom data requests -------------------------------------------------------------------------
def test_custom_request_flow(client, env):
    buyer, admin = env["data@frontier-lab.example"], env["admin@seatrail.example"]
    dana, luis = env["dana@experts.example"], env["luis@experts.example"]
    r = client.post("/api/market/requests", headers=as_user(buyer), json={
        "template": "ap_three_way_match", "quantity": 2, "min_quality": 0.5, "budget_cents": 100000}).json()
    assert r["status"] == "open"
    assert client.post("/api/market/requests", headers=as_user(dana), json={}).status_code == 403
    r = client.post(f"/api/admin/requests/{r['id']}/produce", headers=as_user(admin)).json()
    assert r["status"] == "in_production" and r["progress"]["tasks"] == 2
    req_tasks = db.q("SELECT * FROM tasks WHERE request_id=?", r["id"])
    assert all(t["reward_cents"] == 30000 for t in req_tasks)
    for t, ex in zip(req_tasks, (dana, luis)):
        sid = _work(client, ex, t["id"], seed=t["id"])
        rid = client.post(f"/api/sessions/{sid}/submit", headers=as_user(ex)).json()["record_id"]
        while client.post(f"/api/review/records/{rid}", headers=as_user(admin),
                          json={"approve": True}).json()["status"] == "needs_review":
            db.x("DELETE FROM record_reviews WHERE record_id=? AND reviewer_id=?", rid, admin["id"])
            db.x("INSERT INTO record_reviews (record_id, reviewer_id, approve, created_at) VALUES (?,?,1,?)",
                 rid, buyer["id"], db.now())
    # Exclusive: request records never appear in a public catalog dataset
    from seatrail import market
    try:
        d = market.create_dataset(admin["id"], "pub", "pub", 100, template="ap_three_way_match")
        ids = {x["record_id"] for x in db.q("SELECT record_id FROM dataset_items WHERE dataset_id=?", d["id"])}
        req_ids = {x["id"] for x in db.q("SELECT r.id FROM records r JOIN tasks t ON t.id=r.task_id"
                                         " WHERE t.request_id IS NOT NULL")}
        assert not ids & req_ids
    except market.MarketError:
        pass
    r = client.post(f"/api/admin/requests/{r['id']}/deliver", headers=as_user(admin)).json()
    assert r["status"] == "delivered" and r["dataset_id"]
    mine = client.get("/api/market/purchases", headers=as_user(buyer)).json()
    assert any(p["dataset_id"] == r["dataset_id"] for p in mine)
    assert r["dataset_id"] not in {d["id"] for d in client.get("/api/market/datasets").json()}
    assert client.get(f"/api/market/datasets/{r['dataset_id']}").status_code == 404
    assert client.get(f"/api/market/datasets/{r['dataset_id']}", headers=as_user(buyer)).status_code == 200


# --- leads ---------------------------------------------------------------------------------------
def test_leads(client, env):
    assert client.post("/api/leads", json={"kind": "enterprise", "email": "cfo@acme.example",
                                           "company": "Acme"}).json()["ok"]
    assert client.post("/api/leads", json={"kind": "spam", "email": "x"}).status_code == 400
    rows = client.get("/api/admin/leads", headers=as_user(env["admin@seatrail.example"])).json()
    assert rows[0]["company"] == "Acme"
