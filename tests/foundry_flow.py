"""Drive a foundry task through the pipeline in tests (mock models, no API keys)."""
from __future__ import annotations

from foundry_fixtures import BUGGY, FIXED, code_task

from seatrail import db
from seatrail.foundry import store
from seatrail.foundry.calibrator import calibrate
from seatrail.foundry.calibrator.adapters import MockAdapter
from seatrail.foundry.dispatch import service as dispatch
from seatrail.foundry.models import (ContractBatchIn, ExpertProfileIn, QaDecisionIn, RubricCriterionIn, TaskIn,
                                     TaskStatus)
from seatrail.foundry.sandbox_runner.runner import run_env_sanity_check

SOLVE = [("read", {"path": "calc.py"}), ("write", {"path": "calc.py", "content": FIXED}),
         ("finish", {"summary": "Fixed add() in calc.py"})]
WRONG = [("read", {"path": "calc.py"}), ("write", {"path": "calc.py", "content": BUGGY.replace("a - b", "a * 1")}),
         ("finish", {"summary": "Patched calc.py"})]


def sweet_panel():
    """Pooled 2/8 = 25%: inside the default 15-70% band."""
    return [MockAdapter("mock-a", lambda k: SOLVE if k < 2 else WRONG), MockAdapter("mock-b", lambda k: WRONG)]


def solved_panel():
    return [MockAdapter("mock-a", lambda k: SOLVE), MockAdapter("mock-b", lambda k: SOLVE)]


def new_user(email: str, name: str = "Expert", role: str = "expert") -> int:
    return db.x("INSERT INTO users (org_id, role, name, email, credentials, created_at) VALUES (0,?,?,?,?,?)",
                role, name, email, "", db.now())


def experts(env, **pods):
    """dana authors, luis reviews; both verified for SWE. Optional pods by email local part."""
    ids = {}
    for key in ("dana", "luis"):
        uid = env[f"{key}@experts.example"]["id"]
        dispatch.upsert_profile(uid, ExpertProfileIn(domain_tags=["swe"], per_task_rate_usd=40, hourly_rate_usd=90,
                                                     capacity_cap=3, pod=pods.get(key, "")))
        ids[key] = uid
    return ids


def batch(**kw) -> dict:
    base = dict(client_name="Frontier Lab", deal_type="EVAL", domain="swe")
    base.update(kw)
    return store.create_batch(ContractBatchIn(**base))


def draft(batch_id: int, author_id: int | None, **extra) -> dict:
    t = store.create_task(TaskIn.model_validate(code_task(batch_id, author_id=author_id, **extra)))
    store.add_criterion(t["id"], RubricCriterionIn(kind="PROGRAMMATIC", spec_json={"test": "test_add_fixed"},
                                                   ground_truth_evidence_ref="calc.py:2"))
    return store.get_task(t["id"])


def to_sme_review(batch_id: int, author_id: int | None) -> dict:
    t = draft(batch_id, author_id)
    assert run_env_sanity_check(t["id"])["passed"]
    store.transition(t["id"], TaskStatus.SYNTH_CHECK, author_id)
    out = calibrate.calibrate_task(t["id"], adapters=sweet_panel(), k=4)
    assert out["routed_to"] == TaskStatus.SME_REVIEW, out
    return store.get_task(t["id"])


def approve(task_id: int, reviewer_id: int) -> dict:
    t = store.get_task(task_id)
    dispatch.claim_review(reviewer_id, task_id)
    return dispatch.qa_decide(task_id, reviewer_id, QaDecisionIn(
        decision="APPROVE", checklist={c["criterion_id"]: True for c in t["criteria"]}))


def to_qa_approved(env, batch_id: int | None = None) -> tuple[dict, dict]:
    ids = experts(env)
    b = store.get_batch(batch_id) if batch_id else batch()
    t = to_sme_review(b["id"], ids["dana"])
    approve(t["id"], ids["luis"])
    return store.get_task(t["id"]), ids
