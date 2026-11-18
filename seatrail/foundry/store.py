"""Persistence for the foundry core: batches, source assets, tasks, rubric, assignments, ledger.

All status changes go through `transition()`, which loads a TaskContext from the database and
asks the state machine. Nothing else writes `foundry_tasks.status`.
"""
from __future__ import annotations

from .. import db
from . import state_machine as sm
from .models import (AssignmentIn, ContractBatchIn, LeakageStatus, LedgerKind, PiiScanStatus,
                     RubricCriterionIn, SourceAssetIn, TaskIn, TaskStatus, Verdict)
from .sandbox_runner.grader_isolation import seal, unseal


def audit(task_id: int, kind: str, actor_id: int | None = None, **payload) -> None:
    """Append an immutable TaskAuditEvent (the table refuses UPDATE and DELETE)."""
    db.x("INSERT INTO task_audit_events (task_id, actor_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
         task_id, actor_id, kind, db.dumps(payload), db.now())


def audit_events(task_id: int) -> list[dict]:
    rows = db.q("SELECT * FROM task_audit_events WHERE task_id=? ORDER BY id", task_id)
    for r in rows:
        r["payload"] = db.loads(r["payload"])
    return rows


def pod_of(user_id: int | None) -> str:
    if user_id is None:
        return ""
    r = db.q1("SELECT pod FROM expert_profiles WHERE user_id=?", user_id)
    return r["pod"] if r else ""


# ---------------------------------------------------------------------------
# Batches and source assets
# ---------------------------------------------------------------------------
def create_batch(b: ContractBatchIn) -> dict:
    bid = db.x("INSERT INTO contract_batches (client_name, deal_type, domain, target_pass_rate_min,"
               " target_pass_rate_max, warranty_days, price_per_accepted_task_usd, priority, quoted_cost_per_task_usd,"
               " payment_terms_days, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", b.client_name, b.deal_type,
               b.domain, b.target_pass_rate_min, b.target_pass_rate_max, b.warranty_days,
               b.price_per_accepted_task_usd, b.priority, b.quoted_cost_per_task_usd, b.payment_terms_days, db.now())
    return get_batch(bid)


def get_batch(bid: int) -> dict:
    b = db.q1("SELECT * FROM contract_batches WHERE id=?", bid)
    if not b:
        raise KeyError(f"batch {bid} not found")
    b["task_counts"] = {r["status"]: r["n"] for r in
                        db.q("SELECT status, COUNT(*) n FROM foundry_tasks WHERE batch_id=? GROUP BY status", bid)}
    return b


def create_source_asset(a: SourceAssetIn) -> dict:
    aid = db.x("INSERT INTO source_assets (source_type, raw_uri, content_hash, license_type, ai_training_allowed,"
               " chain_of_title_doc_uri, pii_scan_status, outcome_leakage_status, domain_tag, acquisition_cost_usd,"
               " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", a.source_type, a.raw_uri, a.content_hash,
               a.license_type, int(a.ai_training_allowed), a.chain_of_title_doc_uri, a.pii_scan_status,
               a.outcome_leakage_status, a.domain_tag, a.acquisition_cost_usd, db.now())
    return get_source_asset(aid)


def get_source_asset(aid: int) -> dict:
    a = db.q1("SELECT * FROM source_assets WHERE id=?", aid)
    if not a:
        raise KeyError(f"source asset {aid} not found")
    a["ai_training_allowed"] = bool(a["ai_training_allowed"])
    return a


def update_source_scan(aid: int, pii: PiiScanStatus | None = None, leakage: LeakageStatus | None = None) -> dict:
    get_source_asset(aid)
    if pii is not None:
        db.x("UPDATE source_assets SET pii_scan_status=? WHERE id=?", pii, aid)
    if leakage is not None:
        db.x("UPDATE source_assets SET outcome_leakage_status=? WHERE id=?", leakage, aid)
    return get_source_asset(aid)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
def create_task(t: TaskIn) -> dict:
    get_batch(t.batch_id)
    for aid in t.source_asset_ids:
        get_source_asset(aid)
    now = db.now()
    with db.tx() as c:
        cur = c.execute("INSERT INTO foundry_tasks (batch_id, domain, archetype, title, status, author_id, env,"
                        " sealed, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (t.batch_id, t.domain, t.archetype, t.title, TaskStatus.DRAFT, t.author_id,
                         t.env.model_dump_json(), seal(t.sealed.model_dump()), now, now))
        tid = cur.lastrowid
        for aid in t.source_asset_ids:
            c.execute("INSERT INTO task_source_assets (task_id, asset_id) VALUES (?,?)", (tid, aid))
    audit(tid, "created", t.author_id, batch_id=t.batch_id, archetype=t.archetype)
    return get_task(tid)


def _row(tid: int) -> dict:
    r = db.q1("SELECT * FROM foundry_tasks WHERE id=?", tid)
    if not r:
        raise KeyError(f"task {tid} not found")
    return r


def get_task(tid: int) -> dict:
    """The task as the API shows it. Never includes the sealed bundle."""
    r = _row(tid)
    out = {k: v for k, v in r.items() if k != "sealed"}
    out["env"] = db.loads(r["env"])
    out["source_asset_ids"] = [x["asset_id"] for x in
                               db.q("SELECT asset_id FROM task_source_assets WHERE task_id=?", tid)]
    out["criteria"] = list_criteria(tid)
    out["history"] = db.q("SELECT from_status, to_status, actor_id, reason, created_at FROM task_transitions"
                          " WHERE task_id=? ORDER BY id", tid)
    return out


def task_env(tid: int) -> dict:
    return db.loads(_row(tid)["env"])


def task_sealed(tid: int) -> dict:
    """Decrypted ground truth. Only the grader and the sanity check call this."""
    return unseal(_row(tid)["sealed"])


def update_task_spec(tid: int, env: dict | None = None, sealed: dict | None = None,
                     actor_id: int | None = None) -> dict:
    """Edit a task's environment or ground truth. Only in DRAFT or REWORK; bumps the version so an
    old sanity check or calibration no longer counts."""
    r = _row(tid)
    if r["status"] not in (TaskStatus.DRAFT, TaskStatus.REWORK):
        raise sm.TransitionError(f"a task in {r['status']} cannot be edited; send it to REWORK first",
                                 "not_editable")
    from .models import EnvSpec, SealedSpec
    new_env = EnvSpec.model_validate(env).model_dump_json() if env is not None else r["env"]
    new_sealed = seal(SealedSpec.model_validate(sealed).model_dump()) if sealed is not None else r["sealed"]
    db.x("UPDATE foundry_tasks SET env=?, sealed=?, version=version+1, updated_at=? WHERE id=?",
         new_env, new_sealed, db.now(), tid)
    audit(tid, "edited", actor_id, version=r["version"] + 1, env=env is not None, sealed=sealed is not None)
    return get_task(tid)


def batch_task_ids(batch_id: int) -> list[int]:
    return [r["id"] for r in db.q("SELECT id FROM foundry_tasks WHERE batch_id=? ORDER BY id", batch_id)]


def add_criterion(tid: int, c: RubricCriterionIn, actor_id: int | None = None) -> dict:
    from .dispatch.rules import criterion_problems
    _row(tid)
    problems = criterion_problems(c.kind, c.spec_json, c.ground_truth_evidence_ref)
    if problems:
        raise ValueError("; ".join(problems))
    cid = db.x("INSERT INTO rubric_criteria (task_id, kind, weight, spec_json, ground_truth_evidence_ref, created_at)"
               " VALUES (?,?,?,?,?,?)", tid, c.kind, c.weight, db.dumps(c.spec_json),
               c.ground_truth_evidence_ref, db.now())
    audit(tid, "criterion_added", actor_id, criterion_id=cid, criterion_kind=c.kind)
    return next(x for x in list_criteria(tid) if x["id"] == cid)


def list_criteria(tid: int) -> list[dict]:
    rows = db.q("SELECT * FROM rubric_criteria WHERE task_id=? ORDER BY id", tid)
    for r in rows:
        r["spec_json"] = db.loads(r["spec_json"])
        r["criterion_id"] = f"c{r['id']}"
    return rows


def assign(tid: int, a: AssignmentIn) -> dict:
    """Author/reviewer pairing. Same person or same authoring pod as the author -> refused."""
    r = _row(tid)
    if a.author_id == a.reviewer_id:
        raise sm.TransitionError("author and reviewer must be different people", "conflict_of_interest")
    if r["author_id"] is not None and a.reviewer_id == r["author_id"]:
        raise sm.TransitionError("the task's author cannot review it", "conflict_of_interest")
    author_pod = a.pod or pod_of(r["author_id"] or a.author_id)
    if author_pod and pod_of(a.reviewer_id) == author_pod:
        raise sm.TransitionError("reviewer is in the same authoring pod", "conflict_of_interest")
    for uid in (a.author_id, a.reviewer_id):
        if not db.q1("SELECT 1 FROM users WHERE id=?", uid):
            raise KeyError(f"user {uid} not found")
    with db.tx() as c:
        c.execute("UPDATE sme_assignments SET active=0 WHERE task_id=?", (tid,))
        cur = c.execute("INSERT INTO sme_assignments (task_id, author_id, reviewer_id, pod, created_at)"
                        " VALUES (?,?,?,?,?)", (tid, a.author_id, a.reviewer_id, a.pod, db.now()))
        if r["author_id"] is None:
            c.execute("UPDATE foundry_tasks SET author_id=? WHERE id=?", (a.author_id, tid))
    audit(tid, "assigned", None, author_id=a.author_id, reviewer_id=a.reviewer_id)
    return db.q1("SELECT * FROM sme_assignments WHERE id=?", cur.lastrowid)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------
def context(tid: int) -> sm.TaskContext:
    r = _row(tid)
    sanity = db.q1("SELECT 1 FROM sanity_checks WHERE task_id=? AND task_version=? AND passed=1", tid, r["version"])
    cal = db.q1("SELECT verdict, task_version FROM calibration_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", tid)
    srcs = db.q("SELECT s.* FROM source_assets s JOIN task_source_assets t ON t.asset_id=s.id WHERE t.task_id=?", tid)
    rev = db.q1("SELECT reviewer_id FROM sme_assignments WHERE task_id=? AND active=1 ORDER BY id DESC LIMIT 1", tid)
    from .dispatch.rules import lint_prompt, rubric_problems
    env = db.loads(r["env"])
    problems = lint_prompt(env.get("instructions", ""))["errors"] + rubric_problems(r["archetype"], list_criteria(tid))
    if env.get("profile") == "profile-sheet-accounting":   # legacy task library: prompt lint only
        problems = lint_prompt(env.get("instructions", ""))["errors"]
    reviewer = rev["reviewer_id"] if rev else None
    clash = bool(reviewer and pod_of(r["author_id"]) and pod_of(reviewer) == pod_of(r["author_id"]))
    return sm.TaskContext(
        status=TaskStatus(r["status"]), version=r["version"], sanity_passed_for_version=bool(sanity),
        criteria_count=db.q1("SELECT COUNT(*) n FROM rubric_criteria WHERE task_id=?", tid)["n"],
        latest_verdict=Verdict(r["verdict"]) if r["verdict"] else None,
        verdict_version=cal["task_version"] if cal else None,
        sources=[sm.SourceState(s["id"], PiiScanStatus(s["pii_scan_status"]), LeakageStatus(s["outcome_leakage_status"]),
                                bool(s["ai_training_allowed"]), s["license_type"], s["chain_of_title_doc_uri"],
                                s["gate_status"] == "APPROVED")
                 for s in srcs],
        author_id=r["author_id"], active_reviewer_id=reviewer, authoring_problems=problems,
        reviewer_pod_clash=clash)


def transition(tid: int, to: TaskStatus, actor_id: int | None = None, reason: str = "") -> dict:
    with db.tx() as c:
        ctx = context(tid)
        sm.check(ctx, TaskStatus(to), actor_id)
        c.execute("UPDATE foundry_tasks SET status=?, updated_at=?, rework_reason=CASE WHEN ?='REWORK' THEN ?"
                  " ELSE rework_reason END WHERE id=?", (to, db.now(), to, reason, tid))
        c.execute("INSERT INTO task_transitions (task_id, from_status, to_status, actor_id, reason, created_at)"
                  " VALUES (?,?,?,?,?,?)", (tid, ctx.status, to, actor_id, reason, db.now()))
        c.execute("INSERT INTO task_audit_events (task_id, actor_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  (tid, actor_id, "transition", db.dumps({"from": ctx.status, "to": to, "reason": reason}), db.now()))
    _on_transition(tid, ctx.status, TaskStatus(to), actor_id)
    return get_task(tid)


_HOOKS: list = []


def on_transition(fn) -> None:
    """Register fn(task_id, from_status, to_status, actor_id); used by finance (payouts) and delivery."""
    _HOOKS.append(fn)


def _on_transition(tid, frm, to, actor_id) -> None:
    for fn in _HOOKS:
        fn(tid, frm, to, actor_id)


def set_verdict(tid: int, verdict: Verdict) -> None:
    db.x("UPDATE foundry_tasks SET verdict=?, updated_at=? WHERE id=?", verdict, db.now(), tid)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------
def ledger(kind: LedgerKind, amount_usd: float, task_id: int | None = None, ref: str = "") -> None:
    batch_id = _row(task_id)["batch_id"] if task_id else None
    db.x("INSERT INTO cost_ledger (task_id, batch_id, kind, amount_usd, ref, created_at) VALUES (?,?,?,?,?,?)",
         task_id, batch_id, kind, round(amount_usd, 6), ref, db.now())


def task_cost(tid: int) -> dict:
    rows = db.q("SELECT kind, SUM(amount_usd) s FROM cost_ledger WHERE task_id=? GROUP BY kind", tid)
    by = {r["kind"]: round(r["s"], 6) for r in rows}
    return {"by_kind": by, "total_usd": round(sum(by.values()), 6)}


def import_sheet_task(batch_id: int, sheet_task_id: int, author_id: int | None = None) -> dict:
    """Wrap an existing accounting sandbox task (`tasks` row) as a foundry task, so the current
    task library flows through the same sanity check, calibration and review pipeline."""
    from .models import Archetype, EnvSpec, Profile, SealedSpec
    t = db.q1("SELECT * FROM tasks WHERE id=?", sheet_task_id)
    if not t:
        raise KeyError(f"sheet task {sheet_task_id} not found")
    env = EnvSpec(profile=Profile.SHEET_ACCOUNTING, instructions=t["brief"], sheet_task_id=sheet_task_id,
                  max_steps=60)
    sealed = SealedSpec(grader={"type": "sheet_answer_key", "answer": db.loads(t["answer_key"])})
    return create_task(TaskIn(batch_id=batch_id, domain=t["domain"], archetype=Archetype.E2E_ENV,
                              title=t["title"], author_id=author_id, env=env, sealed=sealed))
