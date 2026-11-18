"""P8: drift re-measurement on a newer panel, STABLE / DRIFTING / SATURATED, and harder evolutions."""
import pytest
from conftest import as_user
from foundry_flow import SOLVE, WRONG, approve, batch, draft, solved_panel, sweet_panel, to_qa_approved

from seatrail import db
from seatrail.foundry import store
from seatrail.foundry.calibrator import adapters, calibrate
from seatrail.foundry.calibrator.adapters import MockAdapter
from seatrail.foundry.delivery import gateway, packager
from seatrail.foundry.drift import monitor
from seatrail.foundry.models import TaskStatus
from seatrail.foundry.sandbox_runner.runner import run_env_sanity_check
from seatrail.foundry.state_machine import TransitionError
from seatrail.foundry.synthesizer import llm
from seatrail.foundry.synthesizer.llm import MockTextModel

S = TaskStatus
CHILD_TESTS = '''from calc import add, mul


def test_mul_negative():
    assert mul(3, -2) == -6


def test_add_still_works():
    assert add(2, 3) == 5
'''
EVOLVED = {
    "title": "Credit lines have the wrong sign",
    "instructions": "Invoices with credit lines (negative quantities) come out with the wrong sign in our "
                    "totals. Track it down in calc and fix it without changing how add behaves.",
    "edits": [{"path": "calc.py", "find": "    return a * b", "replace": "    return a * abs(b)"}],
    "hidden_tests": [{"path": "test_child.py", "content": CHILD_TESTS}],
    "fail_to_pass": ["test_child.py::test_mul_negative"], "pass_to_pass": ["test_child.py::test_add_still_works"],
    "expected": [], "criteria": [{"kind": "PROGRAMMATIC", "question": "Does mul(3, -2) return -6?",
                                  "evidence_ref": "calc.py:6"}]}
OK = {"solvable": True, "issues": []}


def text_models(reply=EVOLVED):
    return MockTextModel("mock-author", {"evolve": reply}), MockTextModel("mock-critic", {"critic": OK})


def half_panel():
    return [MockAdapter("mock-a", lambda k: SOLVE), MockAdapter("mock-b", lambda k: WRONG)]


def test_stable_and_drifting(env):
    t, _ = to_qa_approved(env)
    assert monitor.check_task(t["id"], sweet_panel())["status"] == "STABLE"
    rep = monitor.check_task(t["id"], half_panel())
    assert (rep["status"], rep["baseline_rate"], rep["current_rate"]) == ("DRIFTING", 0.25, 0.5)
    # drift runs never change the task or its calibrated difficulty
    assert store.get_task(t["id"])["status"] == S.QA_APPROVED
    assert monitor.baseline_rate(t["id"]) == 0.25
    assert db.q1("SELECT COUNT(*) n FROM calibration_runs WHERE task_id=? AND purpose='DRIFT'", t["id"])["n"] == 4
    with pytest.raises(TransitionError):  # a DRAFT is not shipped work
        monitor.check_task(draft(t["batch_id"], None, variant=3)["id"], half_panel())


def test_saturated_task_is_evolved_into_a_harder_child(env):
    t, ids = to_qa_approved(env)
    m, c = text_models()
    out = monitor.run([t["id"]], adapters_factory=solved_panel, author_model=m, critic_model=c)
    rep = out["reports"][0]
    assert rep["status"] == "SATURATED" and rep["action"] == "EVOLVED" and out["evolved"] == [rep["child_task_id"]]
    child = store.get_task(rep["child_task_id"])
    assert (child["status"], child["origin"], child["parent_task_id"], child["author_id"]) == \
        (S.DRAFT, "EVOLVED", t["id"], ids["dana"])
    assert "abs(b)" in child["env"]["workspace"]["calc.py"] and "a + b" in child["env"]["workspace"]["calc.py"]
    assert db.q1("SELECT passed FROM sanity_checks WHERE task_id=?", child["id"])["passed"] == 1
    assert "PARENT GRADER" in m.calls[0][1] and "a + b" in m.calls[0][1]   # built on the solved state
    again = monitor.run([t["id"]], adapters_factory=solved_panel, author_model=m, critic_model=c)
    assert again["reports"][0]["action"] == "" and again["evolved"] == []  # one evolution per parent


def test_evolution_that_breaks_nothing_is_rejected(env):
    t, _ = to_qa_approved(env)
    noop = dict(EVOLVED, edits=[{"path": "README.md", "find": "Tiny calculator.", "replace": "Calculator."}])
    m, c = text_models(noop)
    ev = monitor.evolve(t["id"], m, c, 1.0)
    assert ev["child_task_id"] is None and ev["rejected_at"] == "SANITY"
    with pytest.raises(ValueError):
        monitor.evolve(t["id"], m, MockTextModel("mock-author", {}))


def test_evolved_child_ships_to_the_same_client(env):
    b = batch(client_name="Frontier Lab")
    t, ids = to_qa_approved(env, b["id"])
    r = packager.package_batch(b["id"])
    gateway.deliver(r["id"])
    m, c = text_models()
    child_id = monitor.evolve(t["id"], m, c, 1.0)["child_task_id"]
    store.transition(child_id, S.SYNTH_CHECK, ids["dana"])
    out = calibrate.calibrate_task(child_id, adapters=sweet_panel(), k=4)
    assert out["routed_to"] == S.SME_REVIEW
    approve(child_id, ids["luis"])
    child = store.get_task(child_id)
    sim = packager.jaccard(packager.shingles(packager.task_text(child["env"])),
                           packager.shingles(packager.task_text(store.get_task(t["id"])["env"])))
    r2 = packager.package_batch(b["id"])
    assert r2["task_ids"] == [child_id], (sim, r2["excluded"])


def test_drift_api_with_registered_mocks(client, env):
    t, _ = to_qa_approved(env)
    adapters.register("mock-solver", lambda: MockAdapter("mock-solver", lambda k: SOLVE))
    m, c = text_models()
    llm.register("mock-author", lambda: m)
    llm.register("mock-critic", lambda: c)
    admin = as_user(env["admin@seatrail.example"])
    job = client.post("/api/v1/drift/run?sync=true", headers=admin, json={
        "task_ids": [t["id"]], "models": ["mock-solver"], "author_model": "mock-author",
        "critic_model": "mock-critic"}).json()
    assert job["status"] == "done", job
    assert job["result"]["by_status"] == {"SATURATED": 1} and job["result"]["evolved"]
    rows = client.get("/api/v1/drift/reports", headers=admin).json()
    assert rows[0]["current_rate"] == 1.0 and rows[0]["child_task_id"] == job["result"]["evolved"][0]
    assert run_env_sanity_check(rows[0]["child_task_id"])["passed"]
