"""P7: earnings with holdback, payouts, revenue on acceptance, the 60-day warranty and batch P&L."""
import pytest
from conftest import as_user
from foundry_flow import batch, experts, to_qa_approved, to_sme_review

from seatrail import db
from seatrail.foundry import store
from seatrail.foundry.delivery import gateway, packager
from seatrail.foundry.dispatch import service as dispatch
from seatrail.foundry.finance import service as finance
from seatrail.foundry.models import QaDecisionIn, SourceAssetIn, TaskStatus
from seatrail.foundry.state_machine import TransitionError

S = TaskStatus
DAY = 86400


def _delivered(env, n=1, **kw):
    b = batch(price_per_accepted_task_usd=900, quoted_cost_per_task_usd=300, **kw)
    tasks = [to_qa_approved(env, b["id"], variant=i + 1)[0] for i in range(n)]
    r = packager.package_batch(b["id"])
    gateway.deliver(r["id"])
    return b, tasks, r


def _lines(task_id):
    return {(x["role"], x["kind"]): (x["amount_usd"], x["status"])
            for x in db.q("SELECT * FROM payout_lines WHERE task_id=?", task_id)}


def test_qa_approval_books_author_and_reviewer_earnings(env):
    t, ids = to_qa_approved(env)
    assert _lines(t["id"]) == {("REVIEWER", "REVIEW"): (12.0, "PAYABLE"), ("AUTHOR", "BASE"): (32.0, "PAYABLE"),
                               ("AUTHOR", "HOLDBACK"): (8.0, "HELD")}
    assert store.task_cost(t["id"])["by_kind"]["SME_LABOR"] == 52.0
    assert store.task_cost(t["id"])["by_kind"]["COMPUTE"] == pytest.approx(8 * 0.02)


def test_qa_return_is_rework_cost_and_author_paid_once(env):
    ids = experts(env)
    t = to_sme_review(batch()["id"], ids["dana"])
    dispatch.claim_review(ids["luis"], t["id"])
    dispatch.qa_decide(t["id"], ids["luis"], QaDecisionIn(decision="RETURN", defect_tags=["AMBIGUOUS_PROMPT"],
                                                          notes="Which function is wrong?"))
    assert store.task_cost(t["id"])["by_kind"]["REWORK"] == 12.0
    assert ("AUTHOR", "BASE") not in _lines(t["id"])


def test_acceptance_recognises_revenue_releases_holdback_and_pays(env):
    b, (t,), r = _delivered(env)
    assert finance.accept([t["id"]]) == [t["id"]]
    assert _lines(t["id"])[("AUTHOR", "HOLDBACK")] == (8.0, "PAYABLE")
    e = finance.economics(b["id"])
    assert e["revenue_usd"] == 900 and e["funnel"]["accepted"] == 1
    assert e["costs_usd"]["SME_LABOR"] == 52.0 and e["costs_usd"]["MODEL_API"] > 0
    assert e["cost_per_accepted_task_usd"] == e["total_cost_usd"] and not e["over_quote"]
    assert e["gross_margin_usd"] == pytest.approx(900 - e["total_cost_usd"], abs=0.01)
    assert e["warranty"]["reserve_usd"] == pytest.approx(900 * 0.05)
    assert e["liabilities"]["payouts_payable_usd"] == 52.0 and e["receivables"][0]["amount_usd"] == 900
    paid = finance.run_payouts(reference="2026-11")
    assert sorted(p["amount_usd"] for p in paid) == [12.0, 40.0]
    assert finance.pending() == [] and "Dana" in finance.payout_csv("2026-11")


def test_deemed_acceptance_after_window(env):
    b, (t,), r = _delivered(env)
    assert finance.auto_accept() == []
    assert finance.auto_accept(now=db.now() + 15 * DAY) == [t["id"]]
    assert store.get_task(t["id"])["status"] == S.ACCEPTED


def test_valid_claim_credits_revenue_and_claws_back_holdback(env):
    b, (t,), r = _delivered(env)
    finance.accept([t["id"]])
    finance.run_payouts()
    c = finance.file_claim(t["id"], "BAD_VERIFIER", "hidden test accepts a wrong answer for negatives")
    with pytest.raises(TransitionError):
        finance.file_claim(t["id"], "LEAKAGE", "second claim on the same task")
    out = finance.decide_claim(c["id"], env["admin@seatrail.example"]["id"], True, "CREDITED")
    assert out["status"] == "VALID" and store.get_task(t["id"])["status"] == S.REWORK
    assert "warranty [BAD_VERIFIER]" in store.get_task(t["id"])["rework_reason"]
    assert finance.economics(b["id"])["revenue_usd"] == 0
    assert _lines(t["id"])[("AUTHOR", "CLAWBACK")] == (-8.0, "PAYABLE")
    assert finance.run_payouts() == []   # negative balance carries over, nothing is paid


def test_replacement_claim_costs_and_drift_is_not_authors_fault(env):
    b, (t,), r = _delivered(env)
    made = store.task_cost(t["id"])["total_usd"]
    c = finance.file_claim(t["id"], "TOO_EASY", "every model on our panel now solves this one")
    finance.decide_claim(c["id"], env["admin@seatrail.example"]["id"], True, "REPLACED")
    assert store.task_cost(t["id"])["by_kind"]["WARRANTY_REPLACEMENT"] == pytest.approx(made)
    assert _lines(t["id"])[("AUTHOR", "HOLDBACK")] == (8.0, "HELD")  # not forfeited: drift is our risk
    nxt, _ = to_qa_approved(env, b["id"], variant=7)
    assert finance.link_replacement(c["id"], nxt["id"])["replacement_task_id"] == nxt["id"]
    assert store.get_task(nxt["id"])["parent_task_id"] == t["id"]


def test_warranty_window_closes(env):
    b, (t,), r = _delivered(env, warranty_days=60)
    db.x("UPDATE task_transitions SET created_at=created_at-? WHERE task_id=? AND to_status='DELIVERED'", 61 * DAY,
         t["id"])
    with pytest.raises(TransitionError) as e:
        finance.file_claim(t["id"], "LEAKAGE", "the answer is in the README")
    assert e.value.code == "warranty_expired"
    finance.accept([t["id"]])
    with pytest.raises(TransitionError):
        store.transition(t["id"], S.RETURNED)


def test_source_acquisition_is_allocated(env):
    ids = experts(env)
    b = batch()
    from seatrail.foundry.ingestion import compliance_gate
    a = store.create_source_asset(SourceAssetIn(
        source_type="REPO", raw_uri="s3://src/x", license_type="MIT", ai_training_allowed=True,
        chain_of_title_doc_uri="s3://legal/x.pdf", pii_scan_status="CLEAN", outcome_leakage_status="CLEAN"))
    db.x("UPDATE source_assets SET acquisition_cost_usd=120 WHERE id=?", a["id"])
    compliance_gate.approve(a["id"])
    from foundry_flow import draft
    draft(b["id"], ids["dana"], variant=1, source_asset_ids=[a["id"]])
    draft(batch()["id"], ids["dana"], variant=2, source_asset_ids=[a["id"]])
    assert finance.economics(b["id"])["costs_usd"]["SOURCE_ACQUISITION"] == 60.0


def test_finance_api_and_page(client, env):
    b, (t,), r = _delivered(env)
    admin = as_user(env["admin@seatrail.example"])
    assert client.get("/finance").status_code == 200
    assert client.post(f"/api/v1/releases/{r['id']}/accept", headers=admin, json={}).json()["accepted"] == [t["id"]]
    assert client.get("/api/v1/finance/batches", headers=admin).json()[0]["revenue_usd"] == 900
    assert client.get(f"/api/v1/finance/batches/{b['id']}", headers=admin).json()["funnel"]["accepted"] == 1
    me = client.get("/api/v1/workbench/earnings", headers=as_user(env["dana@experts.example"])).json()
    assert me["by_status"]["PAYABLE"] == 40.0
    claim = client.post("/api/v1/warranty/claims", headers=admin,
                        json={"task_id": t["id"], "defect_tag": "UNSOLVABLE", "detail": "spec file it needs is absent"})
    assert claim.status_code == 200
    rej = client.post(f"/api/v1/warranty/claims/{claim.json()['id']}/decide", headers=admin, json={"valid": False})
    assert rej.json()["status"] == "REJECTED" and store.get_task(t["id"])["status"] == S.ACCEPTED
    paid = client.post("/api/v1/finance/payouts/run", headers=admin, json={"reference": "nov"}).json()
    assert len(paid) == 2
    assert "amount_usd" in client.get("/api/v1/finance/payouts/nov.csv", headers=admin).text
    assert client.get("/api/v1/finance/batches", headers=as_user(env["dana@experts.example"])).status_code == 403
