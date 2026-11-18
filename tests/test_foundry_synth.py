"""P5: hybrid synthesis (Audit -> Transform -> Rules -> Devil's Advocate -> Sanity) on mock models."""
import pytest
from conftest import as_user
from foundry_flow import batch, experts

from seatrail import db
from seatrail.foundry import store
from seatrail.foundry.ingestion import compliance_gate, service as ingestion
from seatrail.foundry.models import SourceAssetIn, TaskStatus
from seatrail.foundry.synthesizer import llm, pipeline
from seatrail.foundry.synthesizer.llm import MockTextModel
from seatrail.foundry.state_machine import TransitionError

PRICING = '''def line_total(qty, unit_price):
    return qty * unit_price


def apply_discount(total, tier):
    rates = {"gold": 0.10, "silver": 0.05}
    return round(total * (1 - rates.get(tier, 0.0)), 2)
'''
POLICY = """# Discount policy

Gold customers get 10% off the order total. Silver customers get 5% off.

## Late fees

Invoices paid after 30 days carry a flat late fee of 25 USD.
"""
HIDDEN = '''from pricing import apply_discount, line_total


def test_gold_discount():
    assert apply_discount(200.0, "gold") == 180.0


def test_line_total():
    assert line_total(3, 2.5) == 7.5
'''

CAND = {"path": "pricing.py", "line_start": 5, "line_end": 7, "archetype": "BUG_FIX",
        "idea": "gold discount rate drifts from the policy"}


def bugfix(**over):
    d = {"title": "Gold discount is wrong",
         "instructions": "A gold customer was billed 196.00 on a 200.00 order. Our policy doc says what gold "
                         "customers should get; please fix pricing so invoices match it.",
         "edits": [{"path": "pricing.py", "find": '"gold": 0.10', "replace": '"gold": 0.02'}],
         "hidden_tests": [{"path": "test_hidden_pricing.py", "content": HIDDEN}],
         "fail_to_pass": ["test_hidden_pricing.py::test_gold_discount"],
         "pass_to_pass": ["test_hidden_pricing.py::test_line_total"], "expected": [],
         "criteria": [{"kind": "PROGRAMMATIC", "question": "Does apply_discount(200, 'gold') return 180?",
                       "evidence_ref": "policy.md#Discount policy"}]}
    d.update(over)
    return d


OK_REVIEW = {"solvable": True, "issues": [{"tag": "TOO_EASY", "severity": "MINOR", "detail": "single constant"}]}
BLOCK = {"solvable": True, "issues": [{"tag": "AMBIGUOUS_PROMPT", "severity": "BLOCKER", "detail": "which tier?"}]}


def seed(tmp_path, approve=True):
    src = tmp_path / "seed"
    src.mkdir()
    (src / "pricing.py").write_text(PRICING)
    (src / "policy.md").write_text(POLICY)
    a = ingestion.ingest(SourceAssetIn(source_type="REPO", raw_uri="file://billing", license_type="MIT",
                                       ai_training_allowed=True, chain_of_title_doc_uri="s3://legal/billing.pdf"),
                         folder=str(src), domain_tag="swe")
    if approve:
        compliance_gate.approve(a["id"])
    return a["id"]


def models(transform, critic=OK_REVIEW, cands=(CAND,)):
    return (MockTextModel("mock-author", {"audit": {"candidates": list(cands)}, "transform": transform}),
            MockTextModel("mock-critic", {"critic": critic}, price_as="claude-haiku-5-5"))


def test_accepted_candidate_becomes_synth_draft_owned_by_sme(env, tmp_path):
    ids = experts(env)
    aid, b = seed(tmp_path), batch()
    m, c = models(bugfix())
    out = pipeline.synthesize(aid, b["id"], m, c, author_id=ids["dana"])
    assert out["accepted"] == 1 and out["yield"] == 1.0, out
    t = store.get_task(out["task_ids"][0])
    assert (t["status"], t["origin"], t["author_id"], t["archetype"]) == (TaskStatus.DRAFT, "SYNTH", ids["dana"], "BUG_FIX")
    assert '"gold": 0.02' in t["env"]["workspace"]["pricing.py"] and "test_hidden_pricing.py" not in t["env"]["workspace"]
    assert store.task_sealed(t["id"])["golden"]["files"]["pricing.py"] == PRICING
    assert db.q1("SELECT passed FROM sanity_checks WHERE task_id=?", t["id"])["passed"] == 1
    assert store.task_cost(t["id"])["by_kind"]["MODEL_API"] > 0
    assert "synthesized" in [e["kind"] for e in store.audit_events(t["id"])]
    # the critic saw the agent-visible package, never the hidden tests
    critic_prompt = c.calls[0][1]
    assert "pricing.py" in critic_prompt and "assert apply_discount" not in critic_prompt
    # and the SME can take it straight through the normal gate
    store.transition(t["id"], TaskStatus.SYNTH_CHECK, ids["dana"])


def test_rules_reject_leaks_and_bad_edits(env, tmp_path):
    aid, b = seed(tmp_path), batch()
    leaky = bugfix(instructions='We removed the right gold rate. Restore "gold": 0.10 in pricing.py please, thanks.')
    m, c = models(leaky)
    r = pipeline.synthesize(aid, b["id"], m, c)["results"][0]
    assert r["status"] == "REJECTED" and r["rejected_at"] == "RULES"
    assert any("we removed" in x for x in r["reasons"]) and any("leakage" in x for x in r["reasons"])
    m, c = models(bugfix(edits=[{"path": "pricing.py", "find": "nope", "replace": "x"}]))
    r = pipeline.synthesize(aid, b["id"], m, c)["results"][0]
    assert r["rejected_at"] == "RULES" and "exactly once" in r["reasons"][0]
    assert c.calls == []  # never reached the critic


def test_devils_advocate_blocks_then_revision_passes(env, tmp_path):
    aid, b = seed(tmp_path), batch()
    m, c = models(bugfix(), critic=[BLOCK, OK_REVIEW])
    r = pipeline.synthesize(aid, b["id"], m, c)["results"][0]
    assert r["status"] == "ACCEPTED" and r["revisions"] == 1
    assert "which tier?" in m.calls[-1][1]  # critique fed back into the second transform
    m, c = models(bugfix(), critic=BLOCK)
    r = pipeline.synthesize(aid, b["id"], m, c)["results"][0]
    assert r["status"] == "REJECTED" and r["rejected_at"] == "CRITIC" and "AMBIGUOUS_PROMPT" in r["reasons"][0]
    m, c = models(bugfix(), critic={"solvable": False, "issues": []})
    assert pipeline.synthesize(aid, b["id"], m, c, max_revisions=0)["results"][0]["rejected_at"] == "CRITIC"


def test_sanity_rejects_edits_the_grader_cannot_see(env, tmp_path):
    aid, b = seed(tmp_path), batch()
    noop = bugfix(edits=[{"path": "pricing.py", "find": '"silver": 0.05', "replace": '"silver": 0.06'}])
    m, c = models(noop)
    r = pipeline.synthesize(aid, b["id"], m, c)["results"][0]
    assert r["rejected_at"] == "SANITY" and "empty submission" in r["reasons"][0]
    assert db.q1("SELECT COUNT(*) n FROM foundry_tasks")["n"] == 0
    rejected_spend = db.q1("SELECT SUM(amount_usd) s FROM cost_ledger WHERE batch_id=? AND task_id IS NULL", b["id"])
    assert rejected_spend["s"] > 0  # wasted synthesis still counts against the batch


def test_audit_candidates_are_validated(env, tmp_path):
    aid, b = seed(tmp_path), batch()
    bad = [{**CAND, "path": "missing.py"}, {**CAND, "line_end": 99}, {**CAND, "archetype": "E2E_ENV"}]
    m, c = models(bugfix(), cands=bad)
    out = pipeline.synthesize(aid, b["id"], m, c)
    assert [r["rejected_at"] for r in out["results"]] == ["AUDIT"] * 3 and out["accepted"] == 0


def test_mask_reconstruct_needs_decoy_and_hides_answer(env, tmp_path):
    aid, b = seed(tmp_path), batch(domain="legal")
    late = "Invoices paid after 30 days carry a flat late fee of 25 USD."
    cand = {"path": "policy.md", "line_start": 5, "line_end": 7, "archetype": "MASK_RECONSTRUCT",
            "idea": "late fee clause"}
    mask = {"title": "Restore the late fee clause",
            "instructions": "The late-fee paragraph in our policy got mangled when we migrated wikis. Finance "
                            "remembers it was a flat fee after thirty days. Write the paragraph to late_fee.txt.",
            "edits": [{"path": "policy.md", "find": late, "replace": "[paragraph lost in migration]"}],
            "hidden_tests": [], "fail_to_pass": [], "pass_to_pass": [],
            "expected": [{"path": "late_fee.txt", "content": late}],
            "criteria": [{"kind": "PROGRAMMATIC", "question": "Does late_fee.txt state a 25 USD fee after 30 days?",
                          "evidence_ref": "policy.md#Late fees"}]}
    m, c = models(mask, cands=[cand])
    r = pipeline.synthesize(aid, b["id"], m, c)["results"][0]
    assert r["rejected_at"] == "RULES" and any("DECOY" in x for x in r["reasons"])
    mask["criteria"].append({"kind": "DECOY_NEGATIVE", "question": "Does it add a percentage-based fee?",
                             "evidence_ref": "policy.md#Late fees"})
    m, c = models(mask, cands=[cand])
    r = pipeline.synthesize(aid, b["id"], m, c)["results"][0]
    assert r["status"] == "ACCEPTED", r
    t = store.get_task(r["task_id"])
    assert late not in t["env"]["workspace"]["policy.md"] and t["env"]["profile"] == "profile-doc-office"
    leak = dict(mask, edits=[{"path": "policy.md", "find": "Silver customers get 5% off.", "replace": "Silver: 5%."}])
    m, c = models(leak, cands=[cand])
    assert pipeline.synthesize(aid, b["id"], m, c)["results"][0]["rejected_at"] == "RULES"


def test_guards(env, tmp_path):
    aid, b = seed(tmp_path, approve=False), batch()
    m, c = models(bugfix())
    with pytest.raises(TransitionError):
        pipeline.synthesize(aid, b["id"], m, c)
    with pytest.raises(ValueError):
        pipeline.synthesize(aid, b["id"], m, MockTextModel("mock-author", {}))


def test_anthropic_text_model_request_shape():
    from types import SimpleNamespace
    sent = {}

    class Msgs:
        def create(self, **kw):
            sent.update(kw)
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text='{"candidates": []}')],
                                   usage=SimpleNamespace(input_tokens=1000, output_tokens=100))
    client = SimpleNamespace(beta=SimpleNamespace(messages=Msgs()))
    out, cost = llm.AnthropicTextModel("claude-opus-5-5", client=client).complete("audit", "sys", "p",
                                                                                  pipeline.AUDIT_SCHEMA)
    assert out == {"candidates": []} and cost > 0
    assert sent["output_config"]["format"]["type"] == "json_schema" and sent["thinking"] == {"type": "adaptive"}
    assert sent["fallbacks"] == "default"


def test_synthesis_api(client, env, tmp_path):
    ids = experts(env)
    aid, b = seed(tmp_path), batch()
    m, c = models(bugfix())
    llm.register("mock-author", lambda: m)
    llm.register("mock-critic", lambda: c)
    admin = env["admin@seatrail.example"]
    body = {"asset_id": aid, "batch_id": b["id"], "model": "mock-author", "critic_model": "mock-critic",
            "author_id": ids["dana"]}
    assert client.post("/api/v1/synth/runs?sync=true", headers=as_user(env["dana@experts.example"]),
                       json=body).status_code == 403
    job = client.post("/api/v1/synth/runs?sync=true", headers=as_user(admin), json=body).json()
    assert job["status"] == "done" and job["result"]["accepted"] == 1
    rows = client.get(f"/api/v1/synth/runs?asset_id={aid}", headers=as_user(admin)).json()
    assert rows[0]["status"] == "ACCEPTED" and rows[0]["task_id"] == job["result"]["task_ids"][0]
