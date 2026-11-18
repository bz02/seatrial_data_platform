"""P6: packaging (manifest, datasheet, canary, dedup, final PII scan) and the signed delivery gateway."""
import json
import time
import zipfile
from pathlib import Path

import pytest
from conftest import as_user
from foundry_flow import batch, to_qa_approved

from seatrail import db
from seatrail.foundry import store
from seatrail.foundry.delivery import gateway, packager
from seatrail.foundry.models import TaskStatus
from seatrail.foundry.state_machine import TransitionError

S = TaskStatus


def _approved(env, n=2, **batch_kw):
    b = batch(**batch_kw)
    tasks = [to_qa_approved(env, b["id"], variant=i + 1)[0] for i in range(n)]
    return b, tasks


def test_release_contents_manifest_and_canary(env):
    b, tasks = _approved(env, deal_type="OTS", price_per_accepted_task_usd=900)
    r = packager.package_batch(b["id"])
    assert r["task_count"] == 2 and sorted(r["task_ids"]) == sorted(t["id"] for t in tasks)
    assert all(store.get_task(t["id"])["status"] == S.PACKAGED for t in tasks)
    root = Path(r["path"])
    recs = [json.loads(x) for x in (root / "tasks.jsonl").read_text().splitlines()]
    assert all(x["canary"] == r["canary"] for x in recs)
    assert recs[0]["difficulty"]["panel_pass_rate"] == 0.25 and recs[0]["rubric"][0]["evidence"] == "calc.py:2"
    tasks_blob = (root / "tasks.jsonl").read_text()
    assert "ZETA-HIDDEN-ORACLE-7741" not in tasks_blob           # hidden tests only in ground_truth
    assert "ZETA-HIDDEN-ORACLE-7741" in (root / "ground_truth.jsonl").read_text()
    assert list((root / "trajectories").iterdir())               # OTS deals ship scored trajectories
    sheet = (root / "datasheet.md").read_text()
    assert r["canary"] in sheet and "15% to 70%" in sheet and "mock-a" in sheet
    assert packager.verify_release(r["id"])["ok"]
    with zipfile.ZipFile(r["zip_path"]) as z:
        assert f"release-{r['id']}/manifest.json" in z.namelist()
    (root / "tasks.jsonl").write_text("tampered\n")
    v = packager.verify_release(r["id"])
    assert not v["ok"] and v["mismatched"] == ["tasks.jsonl"]


def test_eval_deals_never_ship_trajectories(env):
    b, _ = _approved(env, n=1, deal_type="EVAL")
    with pytest.raises(ValueError):
        packager.package_batch(b["id"], include_trajectories=True)
    r = packager.package_batch(b["id"])
    assert not (Path(r["path"]) / "trajectories").exists()


def test_duplicates_and_resale_are_held_back(env):
    b = batch()
    a, _ = to_qa_approved(env, b["id"], variant=1)
    twin, _ = to_qa_approved(env, b["id"], variant=1)       # identical content
    r = packager.package_batch(b["id"])
    assert r["task_ids"] == [a["id"]] and "near-duplicate" in r["excluded"][0]["reason"]
    assert store.get_task(twin["id"])["status"] == S.QA_APPROVED
    b2 = batch(client_name="Other Lab")
    copy, _ = to_qa_approved(env, b2["id"], variant=1)
    with pytest.raises(TransitionError) as e:
        packager.package_batch(b2["id"])
    assert e.value.code == "nothing_to_package" and f"duplicate of task {a['id']}" in str(e.value)
    gateway.revoke(r["id"])  # a revoked, undelivered release frees its tasks again
    assert store.get_task(a["id"])["status"] == S.QA_APPROVED
    assert packager.package_batch(b2["id"])["task_ids"] == [copy["id"]]


def test_final_pii_scan_blocks_leaky_task(env):
    b, (t1, t2) = _approved(env)
    leaky = dict(t2["env"])
    leaky["workspace"] = dict(leaky["workspace"], **{"NOTES.md": "Escalate to jane.doe@acme-client.com"})
    db.x("UPDATE foundry_tasks SET env=? WHERE id=?", json.dumps(leaky), t2["id"])
    r = packager.package_batch(b["id"])
    assert r["task_ids"] == [t1["id"]] and "PII" in r["excluded"][0]["reason"]


def test_signed_links_delivery_and_download_log(client, env):
    b, tasks = _approved(env, n=1)
    r = packager.package_batch(b["id"])
    with pytest.raises(PermissionError):
        gateway.download(gateway.link(r["id"])["token"])       # not delivered yet
    out = gateway.deliver(r["id"])
    assert store.get_task(tasks[0]["id"])["status"] == S.DELIVERED and out["release"]["delivered_at"]
    res = client.get(out["link"]["path"])
    assert res.status_code == 200 and res.headers["content-type"] == "application/zip"
    rid, exp = gateway.verify(out["link"]["token"])
    forged = f"{rid}.{exp + 999}.{out['link']['token'].split('.')[2]}"
    assert client.get(f"/api/v1/deliveries/{forged}").status_code == 403
    assert client.get(f"/api/v1/deliveries/{gateway.sign(rid, time.time() - 5)}").status_code == 403
    new = gateway.link(rid)
    time.sleep(1.1)
    newer = gateway.link(rid)
    assert client.get(new["path"]).status_code == 403 and client.get(newer["path"]).status_code == 200
    gateway.revoke(rid, reason="client asked")
    assert client.get(newer["path"]).status_code == 403
    assert store.get_task(tasks[0]["id"])["status"] == S.DELIVERED  # delivered work stays delivered
    log = gateway.downloads(rid)
    assert [x["ok"] for x in log].count(1) == 2 and any("revoked" in x["detail"] for x in log)


def test_release_api(client, env):
    b, _ = _approved(env, n=1)
    admin = as_user(env["admin@seatrail.example"])
    assert client.post(f"/api/v1/batches/{b['id']}/releases", headers=as_user(env["dana@experts.example"]),
                       json={}).status_code == 403
    r = client.post(f"/api/v1/batches/{b['id']}/releases", headers=admin, json={}).json()
    assert client.post(f"/api/v1/releases/{r['id']}/verify", headers=admin).json()["ok"]
    d = client.post(f"/api/v1/releases/{r['id']}/deliver", headers=admin, json={"ttl_days": 1}).json()
    assert client.get(d["link"]["path"]).status_code == 200
    again = client.post(f"/api/v1/releases/{r['id']}/deliver", headers=admin, json={})
    assert again.status_code == 409
    assert client.get(f"/api/v1/releases/{r['id']}", headers=admin).json()["downloads"][0]["ok"] == 1
    empty = client.post(f"/api/v1/batches/{batch()['id']}/releases", headers=admin, json={})
    assert empty.status_code == 409 and empty.json()["detail"]["code"] == "nothing_to_package"
