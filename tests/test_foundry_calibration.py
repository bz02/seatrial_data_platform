"""P3: pass@k / pass^k calibration with deterministic mock models, 0% / 100% / sweet-spot routing,
failure taxonomy and the Loss Analysis report. No API keys needed."""
from types import SimpleNamespace

import pytest
from conftest import as_user
from foundry_fixtures import BUGGY, FIXED, code_task

from seatrail.foundry import store
from seatrail.foundry.calibrator import adapters, calibrate, loss_analyzer
from seatrail.foundry.calibrator.adapters import AnthropicAdapter, MockAdapter
from seatrail.foundry.models import ContractBatchIn, RubricCriterionIn, TaskIn, TaskStatus, Verdict
from seatrail.foundry.sandbox_runner.runner import run_env_sanity_check

SOLVE = [("read", {"path": "calc.py"}), ("write", {"path": "calc.py", "content": FIXED}),
         ("finish", {"summary": "Fixed add() in calc.py"})]
WRONG = [("read", {"path": "calc.py"}), ("write", {"path": "calc.py", "content": BUGGY.replace("a - b", "a * 1")}),
         ("finish", {"summary": "Patched calc.py"})]
LOOP = [("bash", {"command": "ls"})] * 6
NO_READ = [("write", {"path": "calc.py", "content": BUGGY.replace("a - b", "b - a")}), ("finish", {})]
HALLUCINATE = [("read", {"path": "calc.py"}), ("write", {"path": "calc.py", "content": BUGGY}),
               ("finish", {"summary": "As documented in docs/arith_spec.md, subtraction is intended."})]
DELETE = [("read", {"path": "calc.py"}), ("bash", {"command": "rm calc.py"}), ("finish", {})]


def always(steps):
    return lambda k: steps


def first_n(n, good, bad):
    return lambda k: good if k < n else bad


def _ready(lo=0.15, hi=0.70):
    b = store.create_batch(ContractBatchIn(client_name="Frontier Lab", deal_type="EVAL", domain="swe",
                                           target_pass_rate_min=lo, target_pass_rate_max=hi))
    t = store.create_task(TaskIn.model_validate(code_task(b["id"])))
    store.add_criterion(t["id"], RubricCriterionIn(kind="PROGRAMMATIC"))
    assert run_env_sanity_check(t["id"])["passed"]
    store.transition(t["id"], TaskStatus.SYNTH_CHECK)
    return b, t


def test_pass_at_k_estimators():
    assert calibrate.pass_at_k(4, 0, 4) == 0.0
    assert calibrate.pass_at_k(4, 1, 4) == 1.0
    assert calibrate.pass_at_k(4, 2, 1) == 0.5
    assert calibrate.pass_at_k(10, 3, 2) == pytest.approx(1 - 21 / 45)
    assert calibrate.pass_hat_k(4, 4, 4) == 1.0
    assert calibrate.pass_hat_k(4, 3, 4) == 0.0
    assert calibrate.pass_hat_k(10, 5, 2) == pytest.approx(10 / 45)


def test_classify_band():
    assert calibrate.classify(0.0, 0, 0.15, 0.7) == Verdict.TOO_HARD_0PCT
    assert calibrate.classify(0.1, 1, 0.15, 0.7) == Verdict.TOO_HARD_0PCT
    assert calibrate.classify(0.15, 2, 0.15, 0.7) == Verdict.SWEET_SPOT
    assert calibrate.classify(0.7, 7, 0.15, 0.7) == Verdict.SWEET_SPOT
    assert calibrate.classify(0.75, 3, 0.15, 0.7) == Verdict.SATURATED_100PCT


def test_zero_percent_task_routes_to_rework_with_diagnosis(env):
    _, t = _ready()
    out = calibrate.calibrate_task(t["id"], adapters=[MockAdapter("mock-a", always(WRONG)),
                                                      MockAdapter("mock-b", always(LOOP))])
    assert out["verdict"] == Verdict.TOO_HARD_0PCT and out["routed_to"] == TaskStatus.REWORK
    task = store.get_task(t["id"])
    assert task["status"] == TaskStatus.REWORK and "TOOL_LOOP_STALL" in task["rework_reason"]


def test_saturated_task_routes_to_rework_for_harder_evolution(env):
    _, t = _ready()
    out = calibrate.calibrate_task(t["id"], adapters=[MockAdapter("mock-a", always(SOLVE)),
                                                      MockAdapter("mock-b", always(SOLVE))])
    assert out["verdict"] == Verdict.SATURATED_100PCT
    assert store.get_task(t["id"])["rework_reason"].startswith("harder-evolution")
    assert all(r["pass_hat_k"] == 1.0 for r in out["runs"])


def test_sweet_spot_task_goes_to_sme_review(env):
    _, t = _ready()
    out = calibrate.calibrate_task(t["id"], adapters=[MockAdapter("mock-a", first_n(2, SOLVE, WRONG)),
                                                      MockAdapter("mock-b", always(WRONG))], k=4, concurrency=4)
    assert out["verdict"] == Verdict.SWEET_SPOT and out["pass_rate"] == 0.25
    a = next(r for r in out["runs"] if r["model"] == "mock-a")
    assert (a["pass_count"], a["pass_at_1"], a["pass_at_k"], a["pass_hat_k"]) == (2, 0.5, 1.0, 0.0)
    task = store.get_task(t["id"])
    assert task["status"] == TaskStatus.SME_REVIEW and task["verdict"] == Verdict.SWEET_SPOT
    assert store.task_cost(t["id"])["by_kind"]["MODEL_API"] > 0  # model spend lands in the ledger


def test_failure_buckets(env):
    _, t = _ready()
    calibrate.calibrate_task(t["id"], adapters=[
        MockAdapter("m-loop", always(LOOP)), MockAdapter("m-noread", always(NO_READ)),
        MockAdapter("m-halluc", always(HALLUCINATE)), MockAdapter("m-delete", always(DELETE)),
        MockAdapter("m-wrong", always(WRONG))], k=1)
    from seatrail import db
    got = {r["model_name"]: r["failure_bucket"] for r in
           db.q("SELECT c.model_name, r.failure_bucket FROM rollouts r JOIN calibration_runs c ON c.id=r.run_id")}
    assert got == {"m-loop": "TOOL_LOOP_STALL", "m-noread": "PARTIAL_DOC_READ_OMISSION",
                   "m-halluc": "HALLUCINATED_CITATION", "m-delete": "MISSING_DELIVERABLE_FILE",
                   "m-wrong": "VERIFY_PRECISION_MISS"}


def test_model_outage_trips_circuit_breaker(env):
    _, t = _ready()
    out = calibrate.calibrate_task(t["id"], adapters=[MockAdapter("down", always(["!raise"]))], k=4, concurrency=1)
    assert out["circuit_open"] and out["verdict"] is None
    task = store.get_task(t["id"])
    assert task["status"] == TaskStatus.REWORK and "ENVIRONMENT_ERROR" in task["rework_reason"]


def test_traces_are_written_as_jsonl(env):
    _, t = _ready()
    calibrate.calibrate_task(t["id"], adapters=[MockAdapter("mock-a", always(SOLVE))], k=2)
    from seatrail import db
    import json
    for r in db.q("SELECT trace_uri FROM rollouts"):
        lines = [json.loads(x) for x in open(r["trace_uri"]).read().splitlines()]
        assert lines[0]["type"] == "summary" and lines[0]["stop_reason"] == "finished"
        assert any(x["type"] == "tool_result" and x["name"] == "write" for x in lines)


def test_loss_report(env):
    b, t = _ready()
    calibrate.calibrate_task(t["id"], adapters=[MockAdapter("claude-opus-5-5", first_n(1, SOLVE, LOOP)),
                                                MockAdapter("gpt-x", always(HALLUCINATE))], k=4)
    rep = loss_analyzer.batch_report(b["id"], anonymize_models=True)
    assert {m["model"] for m in rep["by_model"]} == {"Model A", "Model B"}
    assert rep["verdicts"] == {"TOO_HARD_0PCT": 1}  # 1/8 = 12.5% < 15%
    dist = {d["bucket"]: d["count"] for d in rep["failure_distribution"]}
    assert dist["TOOL_LOOP_STALL"] == 3 and dist["HALLUCINATED_CITATION"] == 4
    assert 2 <= len(rep["failure_snippets"]) <= 3
    assert all("claude-opus" not in s["excerpt"] and "gpt-x" not in s["excerpt"] for s in rep["failure_snippets"])
    assert rep["sanity"][0]["golden"] == 1.0 and rep["sanity"][0]["empty"] == 0.0
    md = loss_analyzer.to_markdown(rep)
    assert "| Model A |" in md and "Environment sanity proof" in md and "| 100% | 0% |" in md
    html = loss_analyzer.to_html(rep)
    assert html.startswith("<!doctype html>") and 'class="chart"' in html


def test_calibration_api_end_to_end(client, env):
    adapters.register("mock:half", lambda: MockAdapter("mock:half", first_n(2, SOLVE, WRONG)))
    adapters.register("mock:never", lambda: MockAdapter("mock:never", always(WRONG)))
    admin = as_user(env["admin@seatrail.example"])
    b, t = _ready()
    r = client.post("/api/v1/calibration/run?sync=true", headers=admin,
                    json={"batch_id": b["id"], "models": ["mock:half", "mock:never"], "k": 4})
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["status"] == "done", job
    assert job["result"]["tasks"][0]["verdict"] == "SWEET_SPOT"
    assert client.get(f"/api/v1/jobs/{job['id']}", headers=admin).json()["status"] == "done"
    rep = client.get(f"/api/v1/calibration/batch/{b['id']}/report", headers=admin).json()
    assert rep["verdicts"] == {"SWEET_SPOT": 1}
    md = client.get(f"/api/v1/calibration/batch/{b['id']}/report?format=md", headers=admin)
    assert md.headers["content-type"].startswith("text/markdown") and "pass@1" in md.text
    assert client.get(f"/api/v1/calibration/batch/{b['id']}/report?format=html", headers=admin).status_code == 200
    # a real model without a key is refused up front, not queued to fail later
    r = client.post("/api/v1/calibration/run", headers=admin, json={"task_id": t["id"], "models": ["claude-opus-5-5"]})
    assert r.status_code == 400 and "ANTHROPIC_API_KEY" in r.text


def test_anthropic_adapter_request_shape():
    calls = []

    class FakeMessages:
        def create(self, **kw):
            calls.append(kw)
            if len(calls) == 1:
                content = [SimpleNamespace(type="text", text="Reading the file."),
                           SimpleNamespace(type="tool_use", id="tu_1", name="read", input={"path": "calc.py"})]
                return SimpleNamespace(content=content, stop_reason="tool_use",
                                       usage=SimpleNamespace(input_tokens=1000, output_tokens=100))
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="done")], stop_reason="end_turn",
                                   usage=SimpleNamespace(input_tokens=1200, output_tokens=50))

    client = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages()))
    s = AnthropicAdapter("claude-opus-5-5", client=client).session()
    s.start("sys", "fix it", [{"name": "read", "description": "Read", "input_schema": {"type": "object"}}])
    turn = s.act(None)
    assert turn.tool_calls[0].name == "read" and turn.cost_usd == pytest.approx((1000 * 4 + 100 * 20) / 1e6)
    from seatrail.foundry.sandbox_runner.runner import ToolOutcome
    s.act([ToolOutcome("tu_1", "read", "def add...")])
    kw = calls[1]
    assert kw["model"] == "claude-opus-5-5" and kw["thinking"] == {"type": "adaptive"}
    assert kw["messages"][-2]["content"][0] == {"type": "tool_result", "tool_use_id": "tu_1", "content": "def add..."}
