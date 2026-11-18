"""P0: core models, Task state machine guards (409s), separation of duties, versioning."""
import sqlite3

import pytest
from conftest import as_user
from foundry_fixtures import code_task

from seatrail import db
from seatrail.foundry import state_machine as sm
from seatrail.foundry import store
from seatrail.foundry.models import (AssignmentIn, ContractBatchIn, CriterionKind, LeakageStatus, PiiScanStatus,
                                     RubricCriterionIn, SourceAssetIn, TaskIn, TaskStatus, Verdict)
from seatrail.foundry.sandbox_runner.runner import run_env_sanity_check

S = TaskStatus


def _batch(**kw):
    return store.create_batch(ContractBatchIn(client_name="Frontier Lab", deal_type="EVAL", domain="swe", **kw))


def _clean_asset():
    from seatrail.foundry.ingestion import compliance_gate
    a = store.create_source_asset(SourceAssetIn(
        source_type="REPO", raw_uri="s3://src/calc", license_type="MIT", ai_training_allowed=True,
        chain_of_title_doc_uri="s3://legal/calc-cot.pdf", pii_scan_status="CLEAN", outcome_leakage_status="CLEAN"))
    return compliance_gate.approve(a["id"])


def _ready_task(env, assets=()):
    b = _batch()
    t = store.create_task(TaskIn.model_validate(code_task(b["id"], source_asset_ids=list(assets),
                                                          author_id=env["dana@experts.example"]["id"])))
    store.add_criterion(t["id"], RubricCriterionIn(kind=CriterionKind.PROGRAMMATIC, spec_json={"test": "add"}))
    return t


def test_batch_defaults_and_band_validation(env):
    b = _batch()
    assert (b["target_pass_rate_min"], b["target_pass_rate_max"], b["warranty_days"]) == (0.15, 0.70, 60)
    with pytest.raises(ValueError):
        ContractBatchIn(client_name="x", deal_type="OTS", domain="d", target_pass_rate_min=0.8, target_pass_rate_max=0.2)


def test_workspace_paths_must_stay_relative(env):
    bad = code_task(1)
    bad["env"]["workspace"] = {"../etc/passwd": "x"}
    with pytest.raises(ValueError):
        TaskIn.model_validate(bad)


def test_illegal_jumps_are_rejected(env):
    t = _ready_task(env)
    for to in (S.CALIBRATING, S.QA_APPROVED, S.DELIVERED, S.ACCEPTED):
        with pytest.raises(sm.TransitionError):
            store.transition(t["id"], to)
    assert store.get_task(t["id"])["status"] == S.DRAFT


def test_draft_needs_passing_sanity_check(env):
    t = _ready_task(env)
    with pytest.raises(sm.TransitionError) as e:
        store.transition(t["id"], S.SYNTH_CHECK)
    assert e.value.code == "sanity_check_required"
    assert run_env_sanity_check(t["id"])["passed"]
    assert store.transition(t["id"], S.SYNTH_CHECK)["status"] == S.SYNTH_CHECK


def test_editing_bumps_version_and_invalidates_sanity(env):
    t = _ready_task(env)
    run_env_sanity_check(t["id"])
    store.update_task_spec(t["id"], env={**store.task_env(t["id"]), "instructions": "changed"})
    assert store.get_task(t["id"])["version"] == 2
    with pytest.raises(sm.TransitionError):
        store.transition(t["id"], S.SYNTH_CHECK)


def test_calibrating_needs_rubric_and_clean_sources(env):
    b = _batch()
    flagged = store.create_source_asset(SourceAssetIn(source_type="LEGAL_MATTER", raw_uri="s3://m/1",
                                                      outcome_leakage_status="FLAGGED"))
    t = store.create_task(TaskIn.model_validate(code_task(b["id"], source_asset_ids=[flagged["id"]])))
    run_env_sanity_check(t["id"])
    store.transition(t["id"], S.SYNTH_CHECK)
    with pytest.raises(sm.TransitionError) as e:
        store.transition(t["id"], S.CALIBRATING)
    assert e.value.code == "rubric_required"
    store.add_criterion(t["id"], RubricCriterionIn(kind="PROGRAMMATIC"))
    with pytest.raises(sm.TransitionError) as e:
        store.transition(t["id"], S.CALIBRATING)
    assert e.value.code == "source_blocked"
    store.update_source_scan(flagged["id"], leakage=LeakageStatus.CLEAN)
    assert store.transition(t["id"], S.CALIBRATING)["status"] == S.CALIBRATING


def test_sme_review_requires_sweet_spot_verdict(env):
    t = _ready_task(env)
    run_env_sanity_check(t["id"])
    store.transition(t["id"], S.SYNTH_CHECK)
    store.transition(t["id"], S.CALIBRATING)
    with pytest.raises(sm.TransitionError) as e:
        store.transition(t["id"], S.SME_REVIEW)
    assert e.value.code == "calibration_required"


def _to_sme_review(t):
    """Drive a task to SME_REVIEW the way calibration does (verdict + routing)."""
    run_env_sanity_check(t["id"])
    store.transition(t["id"], S.SYNTH_CHECK)
    store.transition(t["id"], S.CALIBRATING)
    db.x("INSERT INTO calibration_runs (task_id, task_version, model_name, k_rollouts, pass_count, pass_at_1, pass_at_k,"
         " pass_hat_k, mean_score, submission_rate, avg_steps, token_cost_usd, verdict, trace_uri, group_id, created_at)"
         " VALUES (?,1,'mock',4,2,0.5,1,0,0.5,1,3,0,'SWEET_SPOT','-','g',0)", t["id"])
    store.set_verdict(t["id"], Verdict.SWEET_SPOT)
    store.transition(t["id"], S.SME_REVIEW)


def test_flagged_source_can_never_reach_qa_approved(env):
    asset = _clean_asset()
    t = _ready_task(env, [asset["id"]])
    _to_sme_review(t)
    store.assign(t["id"], AssignmentIn(author_id=env["dana@experts.example"]["id"],
                                       reviewer_id=env["luis@experts.example"]["id"]))
    store.update_source_scan(asset["id"], leakage=LeakageStatus.FLAGGED)
    with pytest.raises(sm.TransitionError) as e:
        store.transition(t["id"], S.QA_APPROVED)
    assert e.value.code == "source_blocked"
    store.update_source_scan(asset["id"], leakage=LeakageStatus.CLEAN, pii=PiiScanStatus.REDACTED)
    assert store.transition(t["id"], S.QA_APPROVED)["status"] == S.QA_APPROVED


def test_unlicensed_source_blocks_shipping(env):
    asset = store.create_source_asset(SourceAssetIn(source_type="REPO", raw_uri="s3://x", pii_scan_status="CLEAN",
                                                    outcome_leakage_status="CLEAN"))
    t = _ready_task(env, [asset["id"]])
    _to_sme_review(t)
    store.assign(t["id"], AssignmentIn(author_id=env["dana@experts.example"]["id"],
                                       reviewer_id=env["luis@experts.example"]["id"]))
    with pytest.raises(sm.TransitionError, match="not licensed"):
        store.transition(t["id"], S.QA_APPROVED)


def test_author_cannot_review_or_approve_own_task(env):
    dana, luis = env["dana@experts.example"]["id"], env["luis@experts.example"]["id"]
    t = _ready_task(env)
    with pytest.raises(sm.TransitionError):
        store.assign(t["id"], AssignmentIn(author_id=dana, reviewer_id=dana))
    with pytest.raises(sm.TransitionError):
        store.assign(t["id"], AssignmentIn(author_id=luis, reviewer_id=dana))  # dana wrote the task
    _to_sme_review(t)
    with pytest.raises(sm.TransitionError) as e:
        store.transition(t["id"], S.QA_APPROVED)
    assert e.value.code == "reviewer_required"
    store.assign(t["id"], AssignmentIn(author_id=dana, reviewer_id=luis))
    with pytest.raises(sm.TransitionError) as e:
        store.transition(t["id"], S.QA_APPROVED, actor_id=dana)
    assert e.value.code == "conflict_of_interest"
    # the database refuses it too
    with pytest.raises(sqlite3.IntegrityError):
        db.x("INSERT INTO sme_assignments (task_id, author_id, reviewer_id, created_at) VALUES (?,?,?,0)",
             t["id"], dana, dana)


def test_full_lifecycle_to_accepted_and_returned(env):
    dana, luis = env["dana@experts.example"]["id"], env["luis@experts.example"]["id"]
    t = _ready_task(env, [_clean_asset()["id"]])
    _to_sme_review(t)
    store.assign(t["id"], AssignmentIn(author_id=dana, reviewer_id=luis))
    for to in (S.QA_APPROVED, S.PACKAGED, S.DELIVERED, S.RETURNED, S.REWORK):
        store.transition(t["id"], to, actor_id=luis)
    h = store.get_task(t["id"])["history"]
    assert [x["to_status"] for x in h][-5:] == ["QA_APPROVED", "PACKAGED", "DELIVERED", "RETURNED", "REWORK"]


def test_api_maps_illegal_transition_to_409_and_hides_sealed(client, env):
    admin = as_user(env["admin@seatrail.example"])
    b = client.post("/api/v1/batches", json={"client_name": "Lab", "deal_type": "EVAL", "domain": "swe"},
                    headers=admin).json()
    t = client.post("/api/v1/tasks", json=code_task(b["id"]), headers=admin).json()
    assert "sealed" not in t and "ZETA" not in str(t)
    r = client.post(f"/api/v1/tasks/{t['id']}/transition", json={"to": "QA_APPROVED"}, headers=admin)
    assert r.status_code == 409
    r = client.post(f"/api/v1/tasks/{t['id']}/transition", json={"to": "SYNTH_CHECK"}, headers=admin)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "sanity_check_required"
    assert client.post(f"/api/v1/tasks/{t['id']}/sanity-check", headers=admin).json()["passed"]
    assert client.post(f"/api/v1/tasks/{t['id']}/transition", json={"to": "SYNTH_CHECK"},
                       headers=admin).json()["status"] == "SYNTH_CHECK"
    # experts and buyers cannot use the foundry API
    assert client.get(f"/api/v1/tasks/{t['id']}", headers=as_user(env["dana@experts.example"])).status_code == 403
