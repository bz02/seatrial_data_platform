"""Skill-based dispatch, separation of duties and QA review.

Queue order for every expert: client-urgent batches first (batch.priority), then items that came
back from REWORK, then oldest first. An expert only sees work in their verified domain tags and
never more than their capacity cap at once. Reviewers never see their own tasks or tasks written
by someone in their authoring pod.
"""
from __future__ import annotations

from ... import db
from .. import store
from ..models import AssignmentIn, ExpertProfileIn, QaDecisionIn, TaskStatus
from ..state_machine import TransitionError
from .rules import lint_prompt, rubric_problems


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
def upsert_profile(user_id: int, p: ExpertProfileIn) -> dict:
    if not db.q1("SELECT 1 FROM users WHERE id=?", user_id):
        raise KeyError(f"user {user_id} not found")
    tags = sorted({t.strip().upper() for t in p.domain_tags if t.strip()})
    db.x("INSERT INTO expert_profiles (user_id, domain_tags, hourly_rate_usd, per_task_rate_usd, capacity_cap, pod,"
         " active, created_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET domain_tags=excluded.domain_tags,"
         " hourly_rate_usd=excluded.hourly_rate_usd, per_task_rate_usd=excluded.per_task_rate_usd,"
         " capacity_cap=excluded.capacity_cap, pod=excluded.pod, active=excluded.active",
         user_id, db.dumps(tags), p.hourly_rate_usd, p.per_task_rate_usd, p.capacity_cap, p.pod, int(p.active), db.now())
    return profile(user_id)


def profile(user_id: int) -> dict:
    r = db.q1("SELECT * FROM expert_profiles WHERE user_id=?", user_id)
    if not r:
        raise KeyError(f"user {user_id} has no expert profile")
    r["domain_tags"] = db.loads(r["domain_tags"])
    qa = db.q1("SELECT COUNT(*) n, SUM(decision='APPROVE') ok FROM qa_reviews WHERE author_id=?", user_id)
    r["qa_reviews"] = qa["n"]
    r["qa_acceptance_rate"] = round((qa["ok"] or 0) / qa["n"], 4) if qa["n"] else None
    r["active_load"] = load(user_id)
    return r


def load(user_id: int) -> int:
    seeds = db.q1("SELECT COUNT(*) n FROM seed_claims WHERE user_id=? AND task_id IS NULL", user_id)["n"]
    authoring = db.q1("SELECT COUNT(*) n FROM foundry_tasks WHERE author_id=? AND status IN ('DRAFT','REWORK')",
                      user_id)["n"]
    reviewing = db.q1("SELECT COUNT(*) n FROM sme_assignments a JOIN foundry_tasks t ON t.id=a.task_id"
                      " WHERE a.reviewer_id=? AND a.active=1 AND t.status='SME_REVIEW'", user_id)["n"]
    return seeds + authoring + reviewing


def _check_capacity(user_id: int) -> dict:
    p = profile(user_id)
    if not p["active"]:
        raise PermissionError("expert profile is inactive")
    if p["active_load"] >= p["capacity_cap"]:
        raise TransitionError(f"at capacity ({p['active_load']}/{p['capacity_cap']}); finish something first",
                              "at_capacity")
    return p


def _tag_match(domain: str, tags: list[str]) -> bool:
    return domain.strip().upper() in tags


# ---------------------------------------------------------------------------
# Queues
# ---------------------------------------------------------------------------
def queue(user_id: int) -> dict:
    p = profile(user_id)
    tags = p["domain_tags"]
    rework = db.q("SELECT t.id, t.title, t.domain, t.status, t.rework_reason, b.priority, b.client_name, t.updated_at"
                  " FROM foundry_tasks t JOIN contract_batches b ON b.id=t.batch_id"
                  " WHERE t.author_id=? AND t.status IN ('REWORK','DRAFT')"
                  " ORDER BY b.priority DESC, (t.status='REWORK') DESC, t.updated_at", user_id)
    seeds = [s for s in db.q("SELECT s.id, s.source_type, s.raw_uri, s.domain_tag, s.created_at FROM source_assets s"
                             " LEFT JOIN seed_claims c ON c.asset_id=s.id WHERE s.gate_status='APPROVED'"
                             " AND c.asset_id IS NULL ORDER BY s.created_at")
             if s["domain_tag"].upper() in tags]
    my_pod = p["pod"]
    reviews = []
    for t in db.q("SELECT t.id, t.title, t.domain, t.author_id, b.priority, b.client_name, t.updated_at,"
                  " (SELECT COUNT(*) FROM task_transitions x WHERE x.task_id=t.id AND x.to_status='REWORK') reworks,"
                  " (SELECT reviewer_id FROM sme_assignments a WHERE a.task_id=t.id AND a.active=1"
                  "  ORDER BY a.id DESC LIMIT 1) reviewer_id"
                  " FROM foundry_tasks t JOIN contract_batches b ON b.id=t.batch_id WHERE t.status='SME_REVIEW'"):
        if not _tag_match(t["domain"], tags) or t["author_id"] == user_id:
            continue
        if my_pod and store.pod_of(t["author_id"]) == my_pod:
            continue
        if t["reviewer_id"] not in (None, user_id):
            continue
        reviews.append(t)
    reviews.sort(key=lambda t: (-t["priority"], -(t["reviewer_id"] == user_id), -t["reworks"], t["updated_at"]))
    return {"profile": p, "my_tasks": rework, "seeds": seeds, "reviews": reviews}


def claim_seed(user_id: int, asset_id: int) -> dict:
    p = _check_capacity(user_id)
    a = store.get_source_asset(asset_id)
    if a["gate_status"] != "APPROVED":
        raise TransitionError("this source has not passed the compliance gate", "compliance_blocked")
    if a["domain_tag"].upper() not in p["domain_tags"]:
        raise PermissionError(f"seed is tagged {a['domain_tag']}, outside your verified domains")
    if db.q1("SELECT 1 FROM seed_claims WHERE asset_id=?", asset_id):
        raise TransitionError("seed already claimed", "already_claimed")
    db.x("INSERT INTO seed_claims (asset_id, user_id, created_at) VALUES (?,?,?)", asset_id, user_id, db.now())
    return {"asset_id": asset_id, "user_id": user_id}


def link_seed(asset_id: int, task_id: int) -> None:
    db.x("UPDATE seed_claims SET task_id=? WHERE asset_id=?", task_id, asset_id)


def claim_review(user_id: int, task_id: int) -> dict:
    t = store.get_task(task_id)
    if t["status"] != TaskStatus.SME_REVIEW:
        raise TransitionError(f"task is {t['status']}, not waiting for SME review", "not_reviewable")
    p = _check_capacity(user_id)
    if not _tag_match(t["domain"], p["domain_tags"]):
        raise PermissionError(f"task domain {t['domain']} is outside your verified domains")
    if t["author_id"] is None:
        raise TransitionError("task has no author on record", "no_author")
    return store.assign(task_id, AssignmentIn(author_id=t["author_id"], reviewer_id=user_id))


def auto_dispatch_reviews() -> list[dict]:
    """Assign every unassigned SME_REVIEW task to the eligible reviewer with spare capacity, preferring
    the best QA track record and then the lightest load."""
    out = []
    candidates = db.q("SELECT user_id FROM expert_profiles WHERE active=1")
    tasks = db.q("SELECT t.id, t.domain, t.author_id, b.priority FROM foundry_tasks t JOIN contract_batches b"
                 " ON b.id=t.batch_id WHERE t.status='SME_REVIEW' AND NOT EXISTS (SELECT 1 FROM sme_assignments a"
                 " WHERE a.task_id=t.id AND a.active=1) ORDER BY b.priority DESC, t.updated_at")
    for t in tasks:
        pool = []
        for c in candidates:
            p = profile(c["user_id"])
            if c["user_id"] == t["author_id"] or not _tag_match(t["domain"], p["domain_tags"]):
                continue
            if p["pod"] and p["pod"] == store.pod_of(t["author_id"]):
                continue
            if p["active_load"] >= p["capacity_cap"]:
                continue
            pool.append(p)
        if not pool or t["author_id"] is None:
            continue
        best = sorted(pool, key=lambda p: (-(p["qa_acceptance_rate"] or 0), p["active_load"]))[0]
        out.append(store.assign(t["id"], AssignmentIn(author_id=t["author_id"], reviewer_id=best["user_id"])))
    return out


# ---------------------------------------------------------------------------
# QA review
# ---------------------------------------------------------------------------
def qa_decide(task_id: int, reviewer_id: int, d: QaDecisionIn) -> dict:
    t = store.get_task(task_id)
    if t["status"] != TaskStatus.SME_REVIEW:
        raise TransitionError(f"task is {t['status']}, not in SME review", "not_reviewable")
    rev = db.q1("SELECT reviewer_id FROM sme_assignments WHERE task_id=? AND active=1 ORDER BY id DESC LIMIT 1", task_id)
    if not rev or rev["reviewer_id"] != reviewer_id:
        raise PermissionError("only the assigned reviewer can decide this task")
    crit_ids = {c["criterion_id"] for c in t["criteria"]}
    if d.decision == "APPROVE":
        unchecked = crit_ids - {k for k, v in d.checklist.items() if v}
        if unchecked:
            raise ValueError("approve needs every criterion checked as passing: " + ", ".join(sorted(unchecked)))
        if d.defect_tags:
            raise ValueError("an approval cannot carry defect tags; return it for rework instead")
    else:
        if len(d.notes.strip()) < 10 or not d.defect_tags:
            raise ValueError("return for rework needs at least one defect tag and fix notes (10+ characters)")
    db.x("INSERT INTO qa_reviews (task_id, task_version, reviewer_id, author_id, decision, checklist, defect_tags, notes,"
         " created_at) VALUES (?,?,?,?,?,?,?,?,?)", task_id, t["version"], reviewer_id, t["author_id"], d.decision,
         db.dumps(d.checklist), db.dumps([x.value for x in d.defect_tags]), d.notes.strip(), db.now())
    store.audit(task_id, "qa_decision", reviewer_id, decision=d.decision, tags=[x.value for x in d.defect_tags])
    if d.decision == "APPROVE":
        return store.transition(task_id, TaskStatus.QA_APPROVED, reviewer_id, "QA approved")
    reason = "QA returned [" + ", ".join(x.value for x in d.defect_tags) + "]: " + d.notes.strip()
    return store.transition(task_id, TaskStatus.REWORK, reviewer_id, reason)


def authoring_check(task_id: int) -> dict:
    t = store.get_task(task_id)
    return {"lint": lint_prompt(t["env"].get("instructions", "")),
            "rubric_problems": rubric_problems(t["archetype"], t["criteria"])}
