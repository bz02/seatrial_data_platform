import copy

import pytest

from seatrail import market, sandbox, verifier
from seatrail.pipeline import dedup, judge, pii
from seatrail.tasks.accounting import TEMPLATES


def solve(t):
    st = copy.deepcopy(t["state"])
    a = t["answer"]
    for sh, rows in a["cells"].items():
        for rid, cols in rows.items():
            for c, v in cols.items():
                st = sandbox.apply(st, "edit_cell", {"sheet": sh, "row": rid, "column": c, "value": v})
    for k, v in a["fields"].items():
        st = sandbox.apply(st, "set_field", {"field": k, "value": v})
    for e in a["emails"]:
        st = sandbox.apply(st, "send_email", {"to": e["to"], "subject": "s",
                                              "body": " ".join(g[0] for g in e["must_mention_any"])})
    return sandbox.apply(st, "submit", {})


@pytest.mark.parametrize("template", list(TEMPLATES))
def test_answer_key_solves_every_seed(template):
    for seed in range(40):
        t = TEMPLATES[template](seed)
        assert verifier.verify(solve(t), t["answer"], 0.9)["score"] == 1.0
        blank = sandbox.apply(t["state"], "submit", {})
        assert not verifier.verify(blank, t["answer"], 0.9)["passed"]


def test_templates_are_deterministic_and_varied():
    a, b, c = (TEMPLATES["bank_reconciliation"](s) for s in (1, 1, 2))
    assert a == b
    assert a["answer"] != c["answer"]


def test_unsubmitted_work_does_not_pass():
    t = TEMPLATES["expense_audit"](3)
    st = solve(t)
    st["submitted"] = False
    v = verifier.verify(st, t["answer"], 0.9)
    assert v["score"] == 1.0 and not v["passed"]


def test_sandbox_rejects_invalid_actions():
    st = TEMPLATES["ap_three_way_match"](1)["state"]
    with pytest.raises(sandbox.ActionError):
        sandbox.apply(st, "edit_cell", {"sheet": "Invoices", "row": st["sheets"][0]["rows"][0]["id"],
                                        "column": "decision", "value": "pay it"})
    with pytest.raises(sandbox.ActionError):
        sandbox.apply(st, "edit_cell", {"sheet": "Invoices", "row": st["sheets"][0]["rows"][0]["id"],
                                        "column": "total", "value": "1"})  # read-only column
    with pytest.raises(sandbox.ActionError):
        sandbox.apply(st, "rm_rf", {})
    done = sandbox.apply(st, "submit", {})
    with pytest.raises(sandbox.ActionError):
        sandbox.apply(done, "set_field", {"field": "approved_total", "value": 1})


def test_feedback_does_not_leak_answers():
    t = TEMPLATES["bank_reconciliation"](5)
    v = verifier.verify(sandbox.apply(t["state"], "submit", {}), t["answer"], 0.9)
    tips = " ".join(verifier.feedback_for_expert(v))
    assert "outstanding_check" not in tips and "L0" not in tips


def test_pii_redaction():
    r = pii.Redactor(["Meridian Health"])
    out = r.text("Call Jane at (415) 555-0199 or jane@meridian.com about Meridian Health, SSN 123-45-6789, "
                 "card 4111 1111 1111 1111, ref 1234567890123 and ap@vendor.example")
    assert "555-0199" not in out and "jane@meridian.com" not in out and "Meridian Health" not in out
    assert "123-45-6789" not in out and "4111 1111 1111 1111" not in out
    assert "1234567890123" in out            # fails Luhn: not a card, leave it
    assert "ap@vendor.example" in out        # synthetic sandbox domain is safe
    assert r.report()["redactions"] == {"CUSTOM": 1, "PHONE": 1, "EMAIL": 1, "SSN": 1, "CARD": 1}
    assert pii.Redactor().url("https://erp.co/inv?id=88&token=abc#x") == "https://erp.co/inv#x"
    assert pii.Redactor().url("https://erp.co/app#access_token=abc") == "https://erp.co/app"


def test_minhash_near_duplicates():
    a = "B004 is nine dollars off from L012 which looks like transposed digits so it is a book error " * 3
    b = a.replace("nine", "9")
    c = "Hotel at 380 a night exceeds the 250 nightly cap so this line is a lodging policy violation " * 3
    assert dedup.similarity(dedup.signature(a), dedup.signature(b)) >= 0.7
    assert dedup.similarity(dedup.signature(a), dedup.signature(c)) < 0.2
    sig = dedup.signature(a)
    assert dedup.decode(dedup.encode(sig)) == sig


def test_settlement_math(monkeypatch):
    s = market.settle(100_000, 0)
    assert s["expert_pool_cents"] == 20_000 and s["platform_fee_cents"] == 80_000
    s = market.settle(100_000, 42)
    assert s["platform_fee_cents"] == 15_000 and s["seller_net_cents"] == 85_000 and s["expert_pool_cents"] == 0


def _record(thoughts):
    steps = [{"i": i, "t": i, "thought": th, "action": {"tool": "edit_cell", "args": {}}, "observation": "OK"}
             for i, th in enumerate(thoughts)]
    return {"task": {"title": "t", "instructions": "x"}, "steps": steps, "outcome": {"label": "success"},
            "provenance": {"duration_s": 100}}


def test_heuristic_judge_rewards_explained_specific_work():
    good = judge.heuristic(_record([f"Row E0{i} is 312.40 for one night, over the $250 cap, violation." for i in range(6)]),
                           {"passed": True, "score": 1, "checks": []})
    bad = judge.heuristic(_record(["", "", "", "", "", "ok"]), {"passed": False, "score": 0.3, "checks": []})
    assert good["score"] > bad["score"]
    assert "task_not_completed" in bad["flags"]


def test_model_judge_used_when_credentials_present(monkeypatch):
    import json
    import types

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    payload = {"correctness": 5, "specificity": 4, "consistency": 5, "coverage": 4, "teachability": 4, "overall": 4,
               "strengths": ["s"], "issues": [], "flags": [], "feedback_to_expert": "Good."}
    captured = {}

    class FakeMessages:
        def create(self, **kw):
            captured.update(kw)
            return types.SimpleNamespace(stop_reason="end_turn", model=kw["model"],
                                         content=[types.SimpleNamespace(type="text", text=json.dumps(payload))])

    class FakeClient:
        def __init__(self, **kw):
            self.beta = types.SimpleNamespace(messages=FakeMessages())

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    out = judge.judge(_record(["because"] * 3), None, {"judge": "auto"})
    assert out["mode"] == "model" and out["score"] == 0.75
    assert captured["output_config"]["format"]["type"] == "json_schema"
    assert captured["model"] == "claude-opus-5-5"
    # Policy "off" never calls the model, even with credentials.
    assert judge.judge(_record(["x"]), None, {"judge": "off"})["mode"] == "heuristic"
