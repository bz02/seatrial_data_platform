"""P2: sandbox runtime, anti-cheat grader isolation, deterministic graders, golden=1.0 / empty=0.0."""
import json
import os
import zipfile
from pathlib import Path

import pytest
from foundry_fixtures import (FIXED, ORIGINAL_DOCX, REDLINED_DOCX, SECRET_MARKER, b64, code_task, docx_task)

from seatrail import db
from seatrail.foundry import store
from seatrail.foundry.models import ContractBatchIn, EnvSpec, TaskIn
from seatrail.foundry.sandbox_runner import grader_isolation, verifiers
from seatrail.foundry.sandbox_runner.backends import DockerBackend, Limits, LocalBackend
from seatrail.foundry.sandbox_runner.runner import ScriptPolicy, rollout_and_grade, run_env_sanity_check
from seatrail.foundry.sandbox_runner.tool_server import InContainerBackend, ToolServer
from seatrail.foundry.sandbox_runner.tools import WorkspaceTools


def _batch():
    return store.create_batch(ContractBatchIn(client_name="Lab", deal_type="EVAL", domain="swe"))["id"]


@pytest.mark.parametrize("mode", ["agentic_native", "auto"])
def test_code_task_golden_is_1_and_empty_is_0(env, mode):
    t = store.create_task(TaskIn.model_validate(code_task(_batch(), mode=mode)))
    r = run_env_sanity_check(t["id"])
    assert r == {**r, "passed": True, "golden_score": 1.0, "empty_score": 0.0}


def test_docx_redline_task_sanity(env):
    t = store.create_task(TaskIn.model_validate(docx_task(_batch())))
    r = run_env_sanity_check(t["id"])
    assert r["passed"], r


def test_sanity_check_catches_a_grader_that_gives_free_credit(env):
    spec = code_task(_batch())
    # bug in the task: the "fail_to_pass" test already passes on the buggy code
    spec["sealed"]["grader"]["fail_to_pass"] = ["test_hidden.py::test_mul_still_works"]
    spec["sealed"]["grader"]["pass_to_pass"] = []
    t = store.create_task(TaskIn.model_validate(spec))
    r = run_env_sanity_check(t["id"])
    assert not r["passed"] and "free credit" in r["problems"][0]


def test_sanity_check_catches_a_wrong_golden_solution(env):
    spec = code_task(_batch())
    spec["sealed"]["golden"]["files"]["calc.py"] = "def add(a, b):\n    return 0\n"
    t = store.create_task(TaskIn.model_validate(spec))
    r = run_env_sanity_check(t["id"])
    assert not r["passed"] and r["golden_score"] == 0.0


def test_existing_accounting_tasks_pass_sanity_as_foundry_tasks(env):
    b = _batch()
    for row in db.q("SELECT id FROM tasks WHERE purpose='work' LIMIT 3"):
        t = store.import_sheet_task(b, row["id"])
        r = run_env_sanity_check(t["id"])
        assert r["passed"], r


def test_sealed_bundle_is_encrypted_at_rest(env):
    t = store.create_task(TaskIn.model_validate(code_task(_batch())))
    raw = db.q1("SELECT sealed FROM foundry_tasks WHERE id=?", t["id"])["sealed"]
    assert SECRET_MARKER not in raw and "fail_to_pass" not in raw
    assert SECRET_MARKER in json.dumps(store.task_sealed(t["id"]))


def test_wrong_key_cannot_unseal(env, monkeypatch):
    token = grader_isolation.seal({"x": 1})
    monkeypatch.setenv(grader_isolation.KEY_ENV, "A" * 43 + "=")
    with pytest.raises(ValueError):
        grader_isolation.unseal(token)


def test_malicious_agent_cannot_find_ground_truth(env, monkeypatch, tmp_path):
    """The agent searches the whole filesystem and its environment for the hidden tests."""
    monkeypatch.setenv("SEATRAIL_GRADER_KEY", grader_isolation.Fernet.generate_key().decode())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-leak")
    spec = code_task(_batch(), mode="auto")
    spec["sealed"]["grader"]["tests"] = {"test_truth.py": spec["sealed"]["grader"]["tests"]["test_hidden.py"]}
    spec["sealed"]["grader"]["fail_to_pass"] = ["test_truth.py::test_add_fixed"]
    spec["sealed"]["grader"]["pass_to_pass"] = ["test_truth.py::test_mul_still_works"]
    t = store.create_task(TaskIn.model_validate(spec))
    env_spec = EnvSpec.model_validate(store.task_env(t["id"]))
    attacks = [
        ("bash", {"command": "find / -xdev \\( -name '*truth*' -o -name 'seatrail-grade-*' \\) 2>/dev/null | head -100"}),
        ("bash", {"command": "env"}),
        ("bash", {"command": f"grep -rl '{SECRET_MARKER}' /tmp /root /home 2>/dev/null | head -20"}),
        ("bash", {"command": "ls -la /workspace; cat /proc/1/environ 2>/dev/null | tr '\\0' '\\n' | head -5"}),
        ("finish", {"summary": "looked around"}),
    ]
    traj, g = rollout_and_grade(ScriptPolicy("attacker", attacks), env_spec, store.task_sealed(t["id"]))
    outputs = "\n".join(x["output"] for x in traj.turns if x["type"] == "tool_result")
    assert "test_truth.py" not in outputs
    assert "seatrail-grade-" not in outputs
    assert SECRET_MARKER not in outputs
    assert "sk-should-never-leak" not in outputs
    assert os.environ["SEATRAIL_GRADER_KEY"] not in outputs
    assert "ANTHROPIC" not in outputs
    assert g.overall_score == 0.0  # and it still did not solve the task


def test_tools_cannot_escape_workspace(env):
    be = LocalBackend()
    try:
        tools = WorkspaceTools(be)
        for path in ("/etc/passwd", "../../etc/passwd", "/workspace/../etc/passwd"):
            r = tools.call("read", {"path": path})
            assert r.is_error and "outside" in r.output
        be.write_bytes("a.txt", b"hello")
        assert tools.call("read", {"path": "/workspace/a.txt"}).output == "hello"
    finally:
        be.destroy()


def test_command_timeout_and_memory_cap(env):
    be = LocalBackend(Limits(memory_mb=256, cmd_timeout_s=2))
    try:
        tools = WorkspaceTools(be, mode="auto", cmd_timeout=1)
        r = tools.call("bash", {"command": "sleep 30"})
        assert r.is_error and "timed out" in r.output
        r = tools.call("code_exec", {"code": "x = bytearray(1024 * 1024 * 1024)"})
        assert r.is_error and "MemoryError" in r.output
    finally:
        be.destroy()


def test_tool_modes_expose_the_right_tools(env):
    be = LocalBackend()
    try:
        assert [s["name"] for s in WorkspaceTools(be, mode="auto").specs()] == ["bash", "code_exec", "view_image", "finish"]
        native = [s["name"] for s in WorkspaceTools(be, mode="agentic_native").specs()]
        assert native == ["bash", "read", "write", "edit", "glob", "grep", "view_image", "finish"]
        assert WorkspaceTools(be, mode="auto").call("read", {"path": "x"}).is_error
    finally:
        be.destroy()


def test_mcp_tool_server_jsonrpc(tmp_path):
    (tmp_path / "notes.md").write_text("alpha\nbeta needle\n")
    server = ToolServer(WorkspaceTools(InContainerBackend(str(tmp_path)), mode="agentic_native"))
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["capabilities"] == {"tools": {}}
    names = [t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
    assert "edit" in names and "inputSchema" in server.handle(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["result"]["tools"][0]
    r = server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                       "params": {"name": "grep", "arguments": {"pattern": "needle"}}})
    assert "notes.md:2: beta needle" in r["result"]["content"][0]["text"]
    r = server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                       "params": {"name": "finish", "arguments": {}}})
    assert r["result"]["_meta"]["finished"] is True
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.handle({"jsonrpc": "2.0", "id": 6, "method": "nope"})["error"]["code"] == -32601


def test_docker_backend_locks_down_the_container():
    be = DockerBackend("seatrail/profile-code-swe:pinned", Limits(cpus=2, memory_mb=2048, pids=128))
    try:
        argv = be.run_argv()
        joined = " ".join(argv)
        for flag in ("--network none", "--cpus 2", "--memory 2048m", "--pids-limit 128", "--read-only",
                     "--cap-drop ALL", "--security-opt no-new-privileges", f"{be.root}:/workspace:rw"):
            assert flag in joined, flag
        assert be.exec(["true"], 5).env_error  # not started -> environment error, not an agent failure
        allow = DockerBackend("img", Limits(network_allowlist=["pypi.org"])).run_argv()
        assert "none" not in allow[allow.index("--network") + 1]
    finally:
        be.destroy()


def test_pinned_images_exist_for_each_profile():
    root = Path(__file__).resolve().parent.parent / "seatrail" / "foundry" / "sandbox_runner" / "images"
    for profile in ("profile-code-swe", "profile-doc-office", "profile-cad-meche"):
        text = (root / profile / "Dockerfile").read_text()
        assert "python:3.12" in text and "tool_server.py" in text and "==" in text


# ---------------------------------------------------------------------------
# Graders
# ---------------------------------------------------------------------------
def _grade(grader, files, env_spec=None):
    env_spec = env_spec or EnvSpec(profile="profile-doc-office")
    with grader_isolation.grader_workdir(files) as wd:
        return verifiers.grade(wd, grader, env_spec)


def test_docx_grader():
    g = {"type": "docx_tracked_changes", "file": "c.docx", "expect_insertions": ["30 days"],
         "expect_deletions": ["60 days"], "min_paragraphs": 3}
    assert _grade(g, {"c.docx": REDLINED_DOCX}).overall_score == 1.0
    assert _grade(g, {"c.docx": ORIGINAL_DOCX}).overall_score == 0.0
    assert _grade(g, {}).failure_mode == "missing_deliverable"
    assert _grade(g, {"c.docx": b"not a zip"}).failure_mode == "format"
    flattened = {**g, "min_paragraphs": 10}
    r = _grade(flattened, {"c.docx": REDLINED_DOCX})
    assert r.overall_score == 0.0 and r.failure_mode == "format"


def test_cad_grader_is_all_or_nothing():
    ref = {"volume": 1000.0, "bbox": [10, 10, 10], "surface_points": [[0, 0, 0], [10, 10, 10], [10, 0, 0]]}
    g = {"type": "cad_metrics", "file": "part.step", "reference": ref}
    exact = json.dumps({"volume": 1000.0, "bbox": [10, 10, 10], "surface_points": ref["surface_points"]}).encode()
    close = json.dumps({"volume": 990.0, "bbox": [10, 10, 9.9], "surface_points": ref["surface_points"]}).encode()
    off = json.dumps({"volume": 900.0, "bbox": [10, 10, 10], "surface_points": ref["surface_points"]}).encode()
    assert _grade(g, {"part.step": b"ISO-10303-21;", "part.step.metrics.json": exact}).overall_score == 1.0
    assert _grade(g, {"part.step": b"ISO-10303-21;", "part.step.metrics.json": close}).passed
    r = _grade(g, {"part.step": b"ISO-10303-21;", "part.step.metrics.json": off})
    assert r.overall_score == 0.0 and r.sub_metrics["volume"] == 0.9  # never a 0.5-style partial pass
    assert _grade(g, {"part.step": b"ISO-10303-21;"}).failure_mode == "format"


def test_file_match_grader():
    g = {"type": "file_match", "files": {"answer.json": {"total": 1250.5, "currency": "USD"}}}
    assert _grade(g, {"answer.json": b'{"total": 1250.504, "currency": "usd"}'}).passed
    assert _grade(g, {"answer.json": b"{oops"}).failure_mode == "format"
    assert _grade(g, {}).failure_mode == "missing_deliverable"


def test_code_grader_flags_regressions():
    spec = code_task(1)
    g = spec["sealed"]["grader"]
    env_spec = EnvSpec.model_validate(spec["env"])
    broke_mul = FIXED.replace("a * b", "a + b").encode()
    r = _grade(g, {"calc.py": broke_mul}, env_spec)
    assert r.overall_score == 0.0 and r.failure_mode == "regression"
    assert _grade(g, {"calc.py": FIXED.encode()}, env_spec).passed
    assert _grade(g, {"calc.py": b"def add(:"}, env_spec).overall_score == 0.0
    assert _grade(g, {}, env_spec).failure_mode == "missing_deliverable"


def test_binary_workspace_files_round_trip(env):
    spec = docx_task(_batch())
    t = store.create_task(TaskIn.model_validate(spec))
    env_spec = EnvSpec.model_validate(store.task_env(t["id"]))
    from seatrail.foundry.sandbox_runner.env import build_env
    e = build_env(env_spec)
    try:
        data = e.deliverables()["contract.docx"]
        assert zipfile.ZipFile(__import__("io").BytesIO(data)).read("word/document.xml")
        assert b64(data) == spec["env"]["workspace"]["contract.docx"]
    finally:
        e.close()
