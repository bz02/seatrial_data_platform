from conftest import as_user

from seatrail import db, demo


def _run(client, expert, task_id, **kw):
    sid = client.post(f"/api/tasks/{task_id}/start", headers=as_user(expert)).json()["session_id"]
    t = db.q1("SELECT * FROM tasks WHERE id=?", task_id)
    events = demo.scripted_sandbox_events(t, **kw)
    r = client.post(f"/api/sessions/{sid}/events", json={"events": events}, headers=as_user(expert)).json()
    assert r["errors"] == [] or kw.get("accuracy", 1) < 1
    return sid


def test_full_marketplace_flow(client, env):
    dana, luis = env["dana@experts.example"], env["luis@experts.example"]
    admin, buyer = env["admin@seatrail.example"], env["data@frontier-lab.example"]

    all_tasks = client.get("/api/tasks", headers=as_user(dana)).json()
    tasks = [t for t in all_tasks if t["purpose"] == "work"]
    assert len(tasks) == 12 and "answer_key" not in tasks[0] and not tasks[0]["locked"]
    assert sum(t["purpose"] == "qualification" for t in all_tasks) == 3

    sid = _run(client, dana, tasks[0]["id"], seed=1)
    s = client.get(f"/api/sessions/{sid}", headers=as_user(dana)).json()
    assert "answer_key" not in s and "answer" not in s["state"]

    rep = client.post(f"/api/sessions/{sid}/check", headers=as_user(dana)).json()
    assert rep["can_submit"], rep["tips"]
    assert rep["verification"]["passed"] and rep["judge"]["mode"] == "heuristic"
    rid = client.post(f"/api/sessions/{sid}/submit", headers=as_user(dana)).json()["record_id"]

    sid2 = _run(client, luis, tasks[4]["id"], seed=2)
    rid2 = client.post(f"/api/sessions/{sid2}/submit", headers=as_user(luis)).json()["record_id"]

    # Experts can't review; admin can.
    assert client.get("/api/review/queue", headers=as_user(dana)).status_code == 403
    q = client.get("/api/review/queue", headers=as_user(admin)).json()
    assert {r["id"] for r in q} == {rid, rid2}
    for r in (rid, rid2):
        assert client.post(f"/api/review/records/{r}", json={"approve": True}, headers=as_user(admin)).json()["status"] == "approved"

    earn = client.get("/api/me/earnings", headers=as_user(dana)).json()
    assert earn["task_rewards_cents"] == tasks[0]["reward_cents"]

    d = client.post("/api/admin/datasets", json={"name": "DS", "price_cents": 100_001}, headers=as_user(admin)).json()
    assert d["stats"]["records"] == 2
    assert client.post(f"/api/market/datasets/{d['id']}/purchase", headers=as_user(dana)).status_code == 403
    # No download before purchase
    assert client.get(f"/api/market/datasets/{d['id']}/download/sft_chat.jsonl",
                      headers=as_user(buyer)).status_code == 403
    o = client.post(f"/api/market/datasets/{d['id']}/purchase", headers=as_user(buyer)).json()
    assert o["expert_pool_cents"] == 20_000 and o["platform_fee_cents"] == 80_001
    assert client.post(f"/api/market/datasets/{d['id']}/purchase", headers=as_user(buyer)).status_code == 400

    shares = db.q("SELECT expert_id, amount_cents FROM payouts WHERE kind='revenue_share' ORDER BY expert_id")
    assert sum(s["amount_cents"] for s in shares) == 20_000 and len(shares) == 2

    dl = client.get(f"/api/market/datasets/{d['id']}/download/sft_chat.jsonl", headers=as_user(buyer))
    assert dl.status_code == 200
    import json
    lines = [json.loads(l) for l in dl.text.splitlines()]
    assert len(lines) == 2 and lines[0]["messages"][0]["role"] == "system"
    assert any(m.get("tool_calls") for m in lines[0]["messages"])
    rl = client.get(f"/api/market/datasets/{d['id']}/download/rl_environments.jsonl", headers=as_user(buyer))
    assert "initial_state" in rl.text and "answer" not in json.loads(rl.text.splitlines()[0])["initial_state"]

    ledger = client.get("/api/admin/ledger", headers=as_user(admin)).json()
    assert ledger["gross_cents"] == 100_001 and ledger["orders"] == 1


def test_low_quality_work_cannot_be_submitted(client, env):
    dana = env["dana@experts.example"]
    tasks = client.get("/api/tasks", headers=as_user(dana)).json()
    # Correct answers but no narration
    sid = _run(client, dana, tasks[8]["id"], narrate=0.0, seed=3)
    rep = client.post(f"/api/sessions/{sid}/check", headers=as_user(dana)).json()
    assert not rep["can_submit"] and "low_reasoning_coverage" in rep["flags"]
    assert client.post(f"/api/sessions/{sid}/submit", headers=as_user(dana)).status_code == 400
    # Narrated but wrong answers
    sid = _run(client, dana, tasks[9]["id"], accuracy=0.3, seed=4)
    rep = client.post(f"/api/sessions/{sid}/check", headers=as_user(dana)).json()
    assert "verification_failed" in rep["flags"] and not rep["can_submit"]


def test_invalid_events_are_rejected_and_not_logged(client, env):
    dana = env["dana@experts.example"]
    t = client.get("/api/tasks", headers=as_user(dana)).json()[0]
    sid = client.post(f"/api/tasks/{t['id']}/start", headers=as_user(dana)).json()["session_id"]
    r = client.post(f"/api/sessions/{sid}/events", headers=as_user(dana), json={"events": [
        {"type": "edit_cell", "payload": {"sheet": "Bank Statement", "row": "B001", "column": "amount", "value": "0"}},
        {"type": "view", "payload": {"target": "brief"}}]}).json()
    assert r["accepted"] == 1 and len(r["errors"]) == 1
    # Another expert can't read or write this session
    luis = env["luis@experts.example"]
    assert client.get(f"/api/sessions/{sid}", headers=as_user(luis)).status_code == 403


def test_copied_narration_is_flagged(client, env):
    dana, luis = env["dana@experts.example"], env["luis@experts.example"]
    tid = client.get("/api/tasks", headers=as_user(dana)).json()[0]["id"]
    s1 = _run(client, dana, tid, seed=7)
    client.post(f"/api/sessions/{s1}/submit", headers=as_user(dana))
    s2 = _run(client, luis, tid, seed=7)  # same seed => same narration
    rep = client.post(f"/api/sessions/{s2}/check", headers=as_user(luis)).json()
    assert "near_duplicate" in rep["flags"] and not rep["can_submit"]


def test_open_marketplace_is_gated(client, env):
    seller = env["seller@vendor.example"]
    r = client.post("/api/market/seller/listings", headers=as_user(seller),
                    json={"name": "x", "price_cents": 100, "jsonl": "{}"})
    assert r.status_code == 403
