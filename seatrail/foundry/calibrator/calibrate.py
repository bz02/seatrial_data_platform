"""Parallel pass@k / pass^k calibration across a panel of frontier models, with automatic routing.

For each model: K independent rollouts (default 4) in the pinned sandbox, each graded in isolation.
Per task, pooled over the panel:

    no rollout passes                          -> TOO_HARD_0PCT   -> REWORK (with a diagnostic)
    pass rate > batch.target_pass_rate_max     -> SATURATED_100PCT -> REWORK (harder evolution)
    pass rate in [min, max]                    -> SWEET_SPOT      -> SME_REVIEW
    0 < pass rate < min                        -> TOO_HARD_0PCT   -> REWORK (below the band)

Tasks are calibrated from SYNTH_CHECK (moved to CALIBRATING first) or re-run from CALIBRATING.
"""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from math import comb
from pathlib import Path

from ... import config, db
from .. import store
from ..models import EnvSpec, LedgerKind, TaskStatus, Verdict
from ..sandbox_runner.runner import CircuitBreaker, Trajectory, rollout_and_grade
from . import loss_analyzer
from .adapters import ModelAdapter, get_adapter


def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased pass@k (Chen et al. 2021): P(at least one of k samples passes)."""
    if n - c < k:
        return 1.0
    return 1.0 - comb(n - c, k) / comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float:
    """pass^k: P(all k samples pass), the consistency metric buyers ask for on agent tasks."""
    if k > n:
        return 0.0
    return comb(c, k) / comb(n, k)


def classify(pass_rate: float, passes: int, lo: float, hi: float) -> Verdict:
    if passes == 0 or pass_rate < lo:
        return Verdict.TOO_HARD_0PCT
    if pass_rate > hi:
        return Verdict.SATURATED_100PCT
    return Verdict.SWEET_SPOT


def _trace_dir(group: str, task_id: int, model: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in model)
    d = config.DATA_DIR / "traces" / group / f"task-{task_id}" / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_model(task_id: int, spec: EnvSpec, sealed: dict, adapter: ModelAdapter, k: int, concurrency: int,
               breaker: CircuitBreaker, group: str) -> dict:
    sessions = [adapter.session() for _ in range(k)]

    def one(i: int):
        if breaker.open:
            t = Trajectory(policy=adapter.name, stop_reason="circuit_open")
            return i, t, None
        traj, g = rollout_and_grade(sessions[i], spec, sealed)
        breaker.record(g.failure_mode == "environment")
        return i, traj, g

    with ThreadPoolExecutor(max_workers=min(concurrency, k)) as pool:
        outs = sorted(pool.map(one, range(k)), key=lambda x: x[0])

    d = _trace_dir(group, task_id, adapter.name)
    rows = []
    for i, traj, g in outs:
        path = d / f"rollout-{i}.jsonl"
        path.write_text(traj.to_jsonl())
        bucket = loss_analyzer.bucket_for(traj, g, spec) if g is not None and not g.passed else \
            ("ENVIRONMENT_ERROR" if g is None else None)
        rows.append({"k_index": i, "traj": traj, "grader": g, "bucket": bucket, "trace_uri": str(path)})
    graded = [r for r in rows if r["grader"] is not None]
    n = len(rows)
    c = sum(1 for r in graded if r["grader"].passed)
    return {"model": adapter.name, "rows": rows, "n": n, "c": c,
            "mean_score": sum(r["grader"].overall_score for r in graded) / n if n else 0.0,
            "submission_rate": sum(r["traj"].submitted for r in rows) / n if n else 0.0,
            "avg_steps": sum(r["traj"].steps for r in rows) / n if n else 0.0,
            "cost": sum(r["traj"].cost_usd for r in rows), "trace_dir": str(d)}


def calibrate_task(task_id: int, adapters: list[ModelAdapter] | None = None, models: list[str] | None = None,
                   k: int = 4, concurrency: int = 4, actor_id: int | None = None) -> dict:
    adapters = adapters or [get_adapter(m) for m in (models or [])]
    if not adapters:
        raise ValueError("no models on the calibration panel")
    task = store.get_task(task_id)
    if task["status"] == TaskStatus.SYNTH_CHECK:
        store.transition(task_id, TaskStatus.CALIBRATING, actor_id, "calibration started")
    elif task["status"] != TaskStatus.CALIBRATING:
        raise store.sm.TransitionError(f"task is {task['status']}; calibrate from SYNTH_CHECK or CALIBRATING",
                                       "not_calibratable")
    batch = store.get_batch(task["batch_id"])
    lo, hi = batch["target_pass_rate_min"], batch["target_pass_rate_max"]
    spec = EnvSpec.model_validate(task["env"])
    sealed = store.task_sealed(task_id)
    group = uuid.uuid4().hex[:12]
    breaker = CircuitBreaker()
    per_model = [_run_model(task_id, spec, sealed, a, k, concurrency, breaker, group) for a in adapters]

    runs = []
    for m in per_model:
        rate = m["c"] / m["n"] if m["n"] else 0.0
        v = classify(rate, m["c"], lo, hi)
        rid = db.x("INSERT INTO calibration_runs (task_id, task_version, model_name, k_rollouts, pass_count, pass_at_1,"
                   " pass_at_k, pass_hat_k, mean_score, submission_rate, avg_steps, token_cost_usd, verdict, trace_uri,"
                   " group_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   task_id, task["version"], m["model"], m["n"], m["c"], pass_at_k(m["n"], m["c"], 1),
                   pass_at_k(m["n"], m["c"], k), pass_hat_k(m["n"], m["c"], k), m["mean_score"],
                   m["submission_rate"], m["avg_steps"], round(m["cost"], 6), v, m["trace_dir"], group, db.now())
        for r in m["rows"]:
            t, g = r["traj"], r["grader"]
            db.x("INSERT INTO rollouts (run_id, k_index, score, passed, submitted, steps, stop_reason, failure_bucket,"
                 " input_tokens, output_tokens, cost_usd, latency_s, grader, trace_uri) VALUES"
                 " (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rid, r["k_index"], g.overall_score if g else 0.0,
                 int(bool(g and g.passed)), int(t.submitted), t.steps, t.stop_reason, r["bucket"], t.input_tokens,
                 t.output_tokens, round(t.cost_usd, 6), round(t.latency_s, 3), db.dumps(g.model_dump() if g else {}),
                 r["trace_uri"])
        if m["cost"]:
            store.ledger(LedgerKind.MODEL_API, m["cost"], task_id, f"calibration {group} {m['model']}")
        runs.append({"run_id": rid, "model": m["model"], "k": m["n"], "pass_count": m["c"],
                     "pass_at_1": round(pass_at_k(m["n"], m["c"], 1), 4),
                     "pass_at_k": round(pass_at_k(m["n"], m["c"], k), 4),
                     "pass_hat_k": round(pass_hat_k(m["n"], m["c"], k), 4), "verdict": v,
                     "cost_usd": round(m["cost"], 6)})

    total_n = sum(m["n"] for m in per_model)
    total_c = sum(m["c"] for m in per_model)
    rate = total_c / total_n if total_n else 0.0
    if breaker.open:
        store.transition(task_id, TaskStatus.REWORK, actor_id,
                         "ENVIRONMENT_ERROR: calibration stopped after repeated environment failures")
        return {"task_id": task_id, "group_id": group, "verdict": None, "pass_rate": rate, "runs": runs,
                "routed_to": TaskStatus.REWORK, "circuit_open": True}
    verdict = classify(rate, total_c, lo, hi)
    store.set_verdict(task_id, verdict)
    store.audit(task_id, "calibrated", actor_id, group_id=group, verdict=verdict, pass_rate=round(rate, 4),
                models=[m["model"] for m in per_model], k=k)
    if verdict == Verdict.SWEET_SPOT:
        routed, reason = TaskStatus.SME_REVIEW, f"sweet spot: panel pass rate {rate:.0%}"
    elif verdict == Verdict.SATURATED_100PCT:
        routed, reason = TaskStatus.REWORK, f"harder-evolution: panel pass rate {rate:.0%} > {hi:.0%}"
    else:
        diag = loss_analyzer.diagnose([r for m in per_model for r in m["rows"]])
        routed, reason = TaskStatus.REWORK, f"too hard: panel pass rate {rate:.0%} < {lo:.0%}; {diag}"
    store.transition(task_id, routed, actor_id, reason)
    return {"task_id": task_id, "group_id": group, "verdict": verdict, "pass_rate": round(rate, 4),
            "runs": runs, "routed_to": routed, "reason": reason, "circuit_open": False}


def calibrate_batch(batch_id: int, models: list[str] | None = None, adapters_factory=None, k: int = 4,
                    concurrency: int = 4, actor_id: int | None = None) -> dict:
    """Calibrate every task in the batch that is ready (SYNTH_CHECK or CALIBRATING)."""
    out, skipped = [], []
    for tid in store.batch_task_ids(batch_id):
        if store.get_task(tid)["status"] not in (TaskStatus.SYNTH_CHECK, TaskStatus.CALIBRATING):
            skipped.append(tid)
            continue
        adapters = adapters_factory() if adapters_factory else [get_adapter(m) for m in (models or [])]
        out.append(calibrate_task(tid, adapters=adapters, k=k, concurrency=concurrency, actor_id=actor_id))
    return {"batch_id": batch_id, "tasks": out, "skipped": skipped}
