"""Code grader: run hidden pytest files against the agent's repository after the rollout.

grader spec:
    {"type": "pytest",
     "tests": {"test_hidden.py": "<source>"},     # never shown to the agent
     "fail_to_pass": ["test_hidden.py::test_fix"], # must pass after the fix (empty = every test)
     "pass_to_pass": ["test_hidden.py::test_old"], # regressions: must keep passing
     "timeout_s": 120}

overall_score = share of fail_to_pass tests passing, or 0 if any pass_to_pass test regressed.
"""
from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from ...models import CriterionResult, EnvSpec, GraderResult
from ..backends import SAFE_ENV


def _run_pytest(repo: Path, tests_dir: Path, timeout: float) -> tuple[int, dict[str, bool], str]:
    report = tests_dir / "report.xml"
    env = {**SAFE_ENV, "HOME": str(repo), "PYTHONPATH": str(repo)}
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(tests_dir),
                            f"--junitxml={report}", str(tests_dir)], cwd=repo, env=env, capture_output=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, {}, "grader timed out"
    results: dict[str, bool] = {}
    if report.exists():
        for tc in ET.parse(report).getroot().iter("testcase"):
            file = Path((tc.get("file") or tc.get("classname", "").replace(".", "/") + ".py")).name
            name = f"{file}::{tc.get('name')}"
            results[name] = not any(child.tag in ("failure", "error") for child in tc)
    return r.returncode, results, (r.stdout + r.stderr).decode("utf-8", "replace")[-4000:]


def grade(workdir: Path, grader: dict, env: EnvSpec) -> GraderResult:
    repo = workdir / "deliverables"
    missing = [d for d in env.deliverables if not (repo / d).exists()]
    if missing:
        return GraderResult(overall_score=0, passed=False, failure_mode="missing_deliverable",
                            sub_metrics={"missing": float(len(missing))})
    tests_dir = workdir / "hidden_tests"
    tests_dir.mkdir()
    os.chmod(tests_dir, 0o700)
    for name, src in grader.get("tests", {}).items():
        (tests_dir / Path(name).name).write_text(src)
    rc, results, log = _run_pytest(repo, tests_dir, float(grader.get("timeout_s", 120)))
    if rc in (-1, 3, 4) or (rc == 5) or not results:
        # timeout, internal error, usage error, or nothing collected: a broken environment, not a model failure
        mode = "environment" if rc != 2 else "format"
        return GraderResult(overall_score=0, passed=False, failure_mode=mode,
                            sub_metrics={"pytest_exit_code": float(rc)})
    f2p = grader.get("fail_to_pass") or [n for n in results if n not in set(grader.get("pass_to_pass", []))]
    p2p = grader.get("pass_to_pass", [])
    f2p_ok = [n for n in f2p if results.get(n)]
    p2p_ok = [n for n in p2p if results.get(n)]
    f2p_rate = len(f2p_ok) / len(f2p) if f2p else 1.0
    p2p_rate = len(p2p_ok) / len(p2p) if p2p else 1.0
    score = f2p_rate if p2p_rate == 1.0 else 0.0
    crit = [CriterionResult(criterion_id=n, passed=bool(results.get(n)), score=float(bool(results.get(n))),
                            detail="fail_to_pass" if n in f2p else "pass_to_pass") for n in [*f2p, *p2p]]
    mode = None
    if score < 1.0:
        mode = "regression" if p2p_rate < 1.0 else "precision"
    return GraderResult(overall_score=round(score, 4), passed=score == 1.0, failure_mode=mode,
                        sub_metrics={"fail_to_pass": round(f2p_rate, 4), "pass_to_pass": round(p2p_rate, 4),
                                     "pytest_exit_code": float(rc)}, criteria_results=crit)
