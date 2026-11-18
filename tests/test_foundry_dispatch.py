"""P4: SME dispatch by domain and capacity, separation of duties (incl. pods), authoring rules,
QA review and the immutable audit trail."""
import sqlite3

import pytest
from conftest import as_user
from foundry_flow import approve, batch, draft, experts, new_user, to_sme_review

from seatrail import db
from seatrail.foundry import store
from seatrail.foundry.dispatch import rules, service as dispatch
from seatrail.foundry.models import (ExpertProfileIn, QaDecisionIn, RubricCriterionIn, SourceAssetIn, TaskStatus)
from seatrail.foundry.sandbox_runner.runner import run_env_sanity_check
from seatrail.foundry.state_machine import TransitionError

S = TaskStatus


# --- authoring rules ---------------------------------------------------------
def test_prompt_linter_blocks_giveaways_and_warns_on_hints():
    bad = rules.lint_prompt("We removed the tax line; the planted error is in the second sheet. Find it please now.")
    assert any("we removed" in e for e in bad["errors"]) and any("planted error" in e for e in bad["errors"])
    soft = rules.lint_prompt("Something is missing from the bracket. The hole sits at (12.5, 40) on the plate face.")
    assert not soft["errors"]
    assert any("missing" in w for w in soft["warnings"]) and any("coordinates" in w for w in soft["warnings"])
    ok = rules.lint_prompt("Our March close is off by a few hundred dollars against the bank statement. "
                           "Reconcile the cash account and post whatever entries are needed.")
    assert ok == {"errors": [], "warnings": [], "words": ok["words"]}


def test_rubric_rules_evidence_subjective_and_decoys():
    assert rules.criterion_problems("PROGRAMMATIC", {}, "calc.py:2") == []
    assert rules.criterion_problems("PROGRAMMATIC", {}, "contract.docx#Payment") == []
    assert rules.criterion_problems("PROGRAMMATIC", {}, "calc.py:2-9") == []
    assert any("evidence" in p for p in rules.criterion_problems("PROGRAMMATIC", {}, "see the file"))
    assert any("subjective" in p for p in rules.criterion_problems(
        "ATOMIC_JUDGE", {"question": "Is the memo well written?"}, "memo.md:1"))
    assert any("question" in p for p in rules.criterion_problems("ATOMIC_JUDGE", {"text": "x"}, "memo.md:1"))
    crit = [{"criterion_id": "c1", "kind": "ATOMIC_JUDGE", "spec_json": {"question": "Is clause 4 flagged?"},
             "ground_truth_evidence_ref": "msa.docx#4"}]
    assert any("DECOY" in p for p in rules.rubric_problems("MULTI_HOP_FLAW", crit))
    crit.append({"criterion_id": "c2", "kind": "DECOY_NEGATIVE", "spec_json": {"question": "Is clause 7 flagged?"},
                 "ground_truth_evidence_ref": "msa.docx#7"})
    assert rules.rubric_problems("MULTI_HOP_FLAW", crit) == []


def test_bad_criterion_is_refused_and_lint_error_blocks_submission(env):
    ids = experts(env)
    t = draft(batch()["id"], ids["dana"])
    with pytest.raises(ValueError):
        store.add_criterion(t["id"], RubricCriterionIn(kind="ATOMIC_JUDGE", spec_json={"question": "Is it reasonable?"},
                                                       ground_truth_evidence_ref="calc.py:2"))
    env_spec = dict(t["env"], instructions="We removed a plus sign from calc.add, put it back.")
    store.update_task_spec(t["id"], env=env_spec, actor_id=ids["dana"])
    assert run_env_sanity_check(t["id"])["passed"]
    with pytest.raises(TransitionError) as e:
        store.transition(t["id"], S.SYNTH_CHECK, ids["dana"])
    assert e.value.code == "authoring_rules"


# --- dispatch ----------------------------------------------------------------
def test_queue_respects_domain_author_pod_and_priority(env):
    ids = experts(env, dana="pod-a")
    carol = new_user("carol@experts.example", "Carol")
    dispatch.upsert_profile(carol, ExpertProfileIn(domain_tags=["SWE"], pod="pod-a"))
    eve = new_user("eve@experts.example", "Eve")
    dispatch.upsert_profile(eve, ExpertProfileIn(domain_tags=["legal"]))
    low = to_sme_review(batch(priority=0)["id"], ids["dana"])
    high = to_sme_review(batch(priority=5, client_name="Urgent Lab")["id"], ids["dana"])
    assert [t["id"] for t in dispatch.queue(ids["luis"])["reviews"]] == [high["id"], low["id"]]
    assert dispatch.queue(ids["dana"])["reviews"] == []        # own tasks
    assert dispatch.queue(carol)["reviews"] == []              # same pod as the author
    assert dispatch.queue(eve)["reviews"] == []                # wrong domain
    with pytest.raises(PermissionError):
        dispatch.claim_review(eve, low["id"])
    with pytest.raises(TransitionError) as e:
        dispatch.claim_review(carol, low["id"])
    assert e.value.code == "conflict_of_interest"
    with pytest.raises(TransitionError):
        dispatch.claim_review(ids["dana"], low["id"])
    dispatch.claim_review(ids["luis"], low["id"])
    assert dispatch.queue(ids["luis"])["reviews"][0]["id"] == high["id"]  # priority still wins over "mine"


def test_capacity_cap_and_seed_claims(env):
    ids = experts(env)
    dispatch.upsert_profile(ids["luis"], ExpertProfileIn(domain_tags=["SWE"], capacity_cap=1))
    seeds = []
    for i in range(2):
        a = store.create_source_asset(SourceAssetIn(
            source_type="REPO", raw_uri=f"s3://src/{i}", license_type="MIT", ai_training_allowed=True,
            chain_of_title_doc_uri="s3://legal/cot.pdf", pii_scan_status="CLEAN", outcome_leakage_status="CLEAN"))
        db.x("UPDATE source_assets SET domain_tag='swe' WHERE id=?", a["id"])
        seeds.append(a["id"])
    with pytest.raises(TransitionError) as e:
        dispatch.claim_seed(ids["luis"], seeds[0])
    assert e.value.code == "compliance_blocked"
    from seatrail.foundry.ingestion import compliance_gate
    for s in seeds:
        compliance_gate.approve(s)
    assert {s["id"] for s in dispatch.queue(ids["luis"])["seeds"]} == set(seeds)
    dispatch.claim_seed(ids["luis"], seeds[0])
    with pytest.raises(TransitionError) as e:
        dispatch.claim_seed(ids["luis"], seeds[1])
    assert e.value.code == "at_capacity"
    with pytest.raises(TransitionError) as e:
        dispatch.claim_seed(ids["dana"], seeds[0])
    assert e.value.code == "already_claimed"


def test_auto_dispatch_prefers_track_record_and_skips_conflicts(env):
    ids = experts(env, dana="pod-a")
    carol = new_user("carol@experts.example", "Carol")
    dispatch.upsert_profile(carol, ExpertProfileIn(domain_tags=["SWE"], pod="pod-a"))
    t = to_sme_review(batch()["id"], ids["dana"])
    out = dispatch.auto_dispatch_reviews()
    assert [(a["task_id"], a["reviewer_id"]) for a in out] == [(t["id"], ids["luis"])]
    assert dispatch.auto_dispatch_reviews() == []  # already assigned


# --- QA review ---------------------------------------------------------------
def test_qa_return_and_approve_rules(env):
    ids = experts(env)
    t = to_sme_review(batch()["id"], ids["dana"])
    cid = t["criteria"][0]["criterion_id"]
    with pytest.raises(PermissionError):  # not assigned yet
        dispatch.qa_decide(t["id"], ids["luis"], QaDecisionIn(decision="APPROVE", checklist={cid: True}))
    dispatch.claim_review(ids["luis"], t["id"])
    with pytest.raises(ValueError):
        dispatch.qa_decide(t["id"], ids["luis"], QaDecisionIn(decision="APPROVE", checklist={cid: False}))
    with pytest.raises(ValueError):
        dispatch.qa_decide(t["id"], ids["luis"], QaDecisionIn(decision="RETURN", notes="bad"))
    out = dispatch.qa_decide(t["id"], ids["luis"], QaDecisionIn(
        decision="RETURN", defect_tags=["AMBIGUOUS_PROMPT"], notes="Say which function is wrong."))
    assert out["status"] == S.REWORK and "AMBIGUOUS_PROMPT" in out["rework_reason"]
    assert dispatch.queue(ids["dana"])["my_tasks"][0]["status"] == S.REWORK
    assert dispatch.profile(ids["dana"])["qa_acceptance_rate"] == 0.0


def test_approval_moves_to_qa_approved_and_audit_is_immutable(env):
    ids = experts(env)
    t = to_sme_review(batch()["id"], ids["dana"])
    assert approve(t["id"], ids["luis"])["status"] == S.QA_APPROVED
    kinds = [e["kind"] for e in store.audit_events(t["id"])]
    for k in ("created", "criterion_added", "sanity_check", "transition", "calibrated", "assigned", "qa_decision"):
        assert k in kinds, k
    with pytest.raises(sqlite3.IntegrityError):
        db.x("UPDATE task_audit_events SET kind='x' WHERE task_id=?", t["id"])
    with pytest.raises(sqlite3.IntegrityError):
        db.x("DELETE FROM task_audit_events WHERE task_id=?", t["id"])


# --- workbench API -----------------------------------------------------------
def test_workbench_api_flow(client, env):
    ids = experts(env)
    dana, luis = env["dana@experts.example"], env["luis@experts.example"]
    admin = env["admin@seatrail.example"]
    assert client.get("/workbench").status_code == 200
    assert client.get("/api/v1/workbench/queue", headers=as_user(env["data@frontier-lab.example"])).status_code == 403
    t = to_sme_review(batch()["id"], ids["dana"])
    assert client.get(f"/api/v1/workbench/tasks/{t['id']}", headers=as_user(luis)).status_code == 403
    q = client.get("/api/v1/workbench/queue", headers=as_user(luis)).json()
    assert [r["id"] for r in q["reviews"]] == [t["id"]]
    r = client.post(f"/api/v1/workbench/tasks/{t['id']}/claim-review", headers=as_user(dana))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "conflict_of_interest"
    assert client.post(f"/api/v1/workbench/tasks/{t['id']}/claim-review", headers=as_user(luis)).status_code == 200
    body = client.get(f"/api/v1/workbench/tasks/{t['id']}", headers=as_user(luis)).json()
    assert body["rollouts"] and body["sealed"]["grader"]["type"] == "pytest"
    replay = client.get(f"/api/v1/workbench/rollouts/{body['rollouts'][0]['id']}", headers=as_user(luis)).json()
    assert replay["turns"] and replay["summary"]["type"] == "summary"
    r = client.post(f"/api/v1/workbench/tasks/{t['id']}/qa", headers=as_user(luis),
                    json={"decision": "APPROVE", "checklist": {}})
    assert r.status_code == 400
    r = client.post(f"/api/v1/workbench/tasks/{t['id']}/qa", headers=as_user(luis),
                    json={"decision": "APPROVE", "checklist": {c["criterion_id"]: True for c in t["criteria"]}})
    assert r.status_code == 200 and r.json()["status"] == S.QA_APPROVED
    lint = client.post("/api/v1/workbench/lint", headers=as_user(dana),
                       json={"instructions": "Find the planted bug.", "criterion": {"kind": "PROGRAMMATIC"}}).json()
    assert lint["prompt"]["errors"] and lint["criterion"]
    audit = client.get(f"/api/v1/tasks/{t['id']}/audit", headers=as_user(admin)).json()
    assert audit[-1]["kind"] == "transition"


def test_workbench_author_flow_from_seed(client, env, tmp_path, monkeypatch):
    from foundry_fixtures import code_task
    from seatrail.foundry.ingestion import compliance_gate, service as ingestion
    ids = experts(env)
    dana = env["dana@experts.example"]
    src = tmp_path / "seed"
    src.mkdir()
    (src / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    a = ingestion.ingest(SourceAssetIn(source_type="REPO", raw_uri="file://seed", license_type="MIT",
                                       ai_training_allowed=True, chain_of_title_doc_uri="s3://legal/c.pdf"),
                         folder=str(src), domain_tag="swe")
    compliance_gate.approve(a["id"])
    b = batch()
    spec = code_task(b["id"], source_asset_ids=[a["id"]])
    assert client.post("/api/v1/workbench/tasks", headers=as_user(dana), json=spec).status_code == 403
    assert client.post(f"/api/v1/workbench/seeds/{a['id']}/claim", headers=as_user(dana)).status_code == 200
    assert client.get(f"/api/v1/workbench/seeds/{a['id']}/files", headers=as_user(dana)).json() == ["calc.py"]
    t = client.post("/api/v1/workbench/tasks", headers=as_user(dana), json=spec).json()
    assert t["author_id"] == ids["dana"]
    r = client.post(f"/api/v1/workbench/tasks/{t['id']}/criteria", headers=as_user(dana),
                    json={"kind": "PROGRAMMATIC", "ground_truth_evidence_ref": "calc.py:2"})
    assert r.status_code == 200
    r = client.post(f"/api/v1/workbench/tasks/{t['id']}/submit", headers=as_user(dana)).json()
    assert r["submitted"] and r["task"]["status"] == S.SYNTH_CHECK
    assert dispatch.load(ids["dana"]) == 0  # seed is now linked to the task, which left DRAFT
