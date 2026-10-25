import json
from pathlib import Path

from conftest import as_user

from seatrail import db, demo

BASE = "http://localhost:8000"


def _token(client, user):
    return client.post("/api/ent/consent", headers=as_user(user)).json()["capture_token"]


def _cap(token):
    return {"Authorization": f"Bearer {token}"}


def test_capture_requires_consent_and_token(client, env):
    sarah = env["sarah@halcyon.example"]
    assert client.get("/api/capture/config").status_code == 401
    assert client.get("/api/capture/config", headers=_cap("nope")).status_code == 401
    tok = _token(client, sarah)
    cfg = client.get("/api/capture/config", headers=_cap(tok)).json()
    assert cfg["consented"] and "localhost" in cfg["allowed_hosts"]
    client.delete("/api/ent/consent", headers=as_user(sarah))
    assert client.get("/api/capture/config", headers=_cap(tok)).status_code == 401  # token revoked
    # Only employees can consent
    assert client.post("/api/ent/consent", headers=as_user(env["tom@halcyon.example"])).status_code == 403


def test_full_enterprise_flow(client, env):
    sarah, ben = env["sarah@halcyon.example"], env["ben@halcyon.example"]
    tom, mei, priya = env["tom@halcyon.example"], env["mei@halcyon.example"], env["priya@halcyon.example"]
    tok = _token(client, sarah)

    sid = client.post("/api/capture/sessions", headers=_cap(tok), json={"goal": "", "workflow": ""}).json()["session_id"]
    events = demo.scripted_capture_events(BASE, 1)
    # Plus an event from a host that is not allowlisted, and an unknown event type
    events.append({"type": "click", "ts": 99, "payload": {"page": {"url": "https://mail.google.com/x"}, "target": {}}})
    events.append({"type": "screenshot", "ts": 99, "payload": {}})
    r = client.post(f"/api/capture/sessions/{sid}/events", headers=_cap(tok), json={"events": events}).json()
    assert r["dropped"] == 2 and r["accepted"] == len(events) - 2
    client.post(f"/api/capture/sessions/{sid}/stop", headers=_cap(tok))

    # Other employees can't see or submit it
    assert client.get(f"/api/ent/sessions/{sid}", headers=as_user(ben)).status_code == 403

    view = client.get(f"/api/ent/sessions/{sid}", headers=as_user(sarah)).json()
    preview = json.dumps(view["preview"])
    assert "Halcyon" not in preview and "415-555-0199" not in preview and "ap@vendor-real.com" not in preview
    assert "[MASKED]" in preview

    # Without a goal the episode can't be submitted
    rep = client.post(f"/api/ent/sessions/{sid}/check", headers=as_user(sarah)).json()
    assert "missing_goal" in rep["flags"] and not rep["can_submit"]

    # Employee deletes the memo step and labels the episode
    memo = next(e for e in view["events"] if e["summary"].startswith("Typed into Memo"))
    client.patch(f"/api/ent/sessions/{sid}", headers=as_user(sarah), json={
        "goal": "Enter a vendor invoice and route for approval", "workflow": "ap_invoice_processing",
        "outcome": "success", "delete_seqs": [memo["seq"]]})
    row = db.q1("SELECT payload, deleted FROM events WHERE session_id=? AND seq=?", sid, memo["seq"])
    assert row["deleted"] == 1 and row["payload"] == "{}"  # content erased, not just hidden

    rep = client.post(f"/api/ent/sessions/{sid}/check", headers=as_user(sarah)).json()
    assert rep["can_submit"], rep["tips"]
    assert rep["judge"]["mode"] == "heuristic" and "disabled by policy" in rep["judge"]["note"]
    rid = client.post(f"/api/ent/sessions/{sid}/submit", headers=as_user(sarah)).json()["record_id"]

    # Employees and ML engineers can't approve; the steward can
    assert client.post(f"/api/review/records/{rid}", headers=as_user(mei), json={"approve": True}).status_code == 403
    assert client.post(f"/api/review/records/{rid}", headers=as_user(tom), json={"approve": True}).json()["status"] == "approved"

    # Marketplace admin (other org) can't see enterprise records
    admin = env["admin@seatrail.example"]
    assert client.get(f"/api/review/records/{rid}", headers=as_user(admin)).status_code == 404
    assert all(r["id"] != rid for r in client.get("/api/review/queue?status=approved", headers=as_user(admin)).json())

    # Export
    assert client.post("/api/ent/exports", headers=as_user(sarah), json={}).status_code == 403
    ex = client.post("/api/ent/exports", headers=as_user(mei), json={"format": "both"}).json()
    assert ex["records"] == 1 and "sft_chat.jsonl" in ex["files"] and "rl_environments.jsonl" not in ex["files"]
    chat = json.loads((Path(ex["path"]) / "sft_chat.jsonl").read_text().splitlines()[0])
    names = [m["tool_calls"][0]["function"]["name"] for m in chat["messages"] if m.get("tool_calls")]
    assert names[0] == "navigate" and "type" in names
    args = [m["tool_calls"][0]["function"]["arguments"] for m in chat["messages"] if m.get("tool_calls")]
    assert not any("Memo" in a for a in args)  # deleted step stayed deleted
    f = client.get(f"/api/ent/exports/{ex['id']}/datacard.md", headers=as_user(mei))
    assert f.status_code == 200 and "Records: 1" in f.text

    # Audit log has the trail and only the org admin can read it
    assert client.get("/api/ent/audit", headers=as_user(tom)).status_code == 403
    actions = [a["action"] for a in client.get("/api/ent/audit", headers=as_user(priya)).json()]
    for a in ("consent.given", "capture.start", "capture.delete_steps", "record.submit", "record.approved",
              "export.create", "export.download"):
        assert a in actions, a


def test_policy_controls(client, env):
    sarah, priya = env["sarah@halcyon.example"], env["priya@halcyon.example"]
    assert client.put("/api/ent/policy", headers=as_user(sarah), json={"judge": "auto"}).status_code == 403
    assert client.put("/api/ent/policy", headers=as_user(priya), json={"bogus": 1}).status_code == 400
    client.put("/api/ent/policy", headers=as_user(priya), json={"capture_input_values": False,
                                                                 "allowed_hosts": ["erp.halcyon.internal"]})
    tok = _token(client, sarah)
    sid = client.post("/api/capture/sessions", headers=_cap(tok), json={}).json()["session_id"]
    ok = {"url": "https://erp.halcyon.internal/ap", "title": "AP"}
    r = client.post(f"/api/capture/sessions/{sid}/events", headers=_cap(tok), json={"events": [
        {"type": "input", "ts": 1, "payload": {"page": ok, "target": {"label": "Amount"}, "value": "1234.00"}},
        {"type": "input", "ts": 2, "payload": {"page": {"url": "http://localhost:8000/demo-erp"}, "value": "x"}},
    ]}).json()
    assert r == {"accepted": 1, "dropped": 1}
    p = json.loads(db.q1("SELECT payload FROM events WHERE session_id=?", sid)["payload"])
    assert p["value"] == "" and p["masked"] is True


def test_discard_and_retention(client, env):
    sarah, priya = env["sarah@halcyon.example"], env["priya@halcyon.example"]
    tok = _token(client, sarah)
    sid = client.post("/api/capture/sessions", headers=_cap(tok), json={}).json()["session_id"]
    client.post(f"/api/capture/sessions/{sid}/events", headers=_cap(tok),
                json={"events": demo.scripted_capture_events(BASE, 2)})
    client.post(f"/api/ent/sessions/{sid}/discard", headers=as_user(sarah))
    assert db.q1("SELECT COUNT(*) n FROM events WHERE session_id=?", sid)["n"] == 0

    sid2 = client.post("/api/capture/sessions", headers=_cap(tok), json={}).json()["session_id"]
    client.post(f"/api/capture/sessions/{sid2}/events", headers=_cap(tok),
                json={"events": demo.scripted_capture_events(BASE, 3)})
    db.x("UPDATE sessions SET started_at=started_at-200*86400 WHERE id=?", sid2)
    r = client.post("/api/ent/retention/purge", headers=as_user(priya)).json()
    assert r["sessions_purged"] >= 1
    assert db.q1("SELECT COUNT(*) n FROM events WHERE session_id=?", sid2)["n"] == 0


def test_demo_seed_runs(env):
    out = demo.seed_full(BASE)
    assert len(out["sandbox_records"]) == 9 and len(out["capture_records"]) == 3
