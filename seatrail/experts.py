"""Expert vetting and reputation for the marketplace.

Instead of a résumé screen or an AI interview, experts prove skill on the real thing: a graded
qualification task per task family, checked against a hidden answer key. Tier then tracks the
quality of their approved work.
"""
from __future__ import annotations

from . import config, db
from .pipeline import quality

QUAL_SEED_BASE = 9000
MAX_QUAL_ATTEMPTS = 3
SENIOR_MIN_APPROVED = 10
SENIOR_MIN_QUALITY = 0.85
SENIOR_MIN_APPROVAL_RATE = 0.9


def ensure_qualification_tasks(templates) -> None:
    from . import tasks
    for t in templates:
        tid = tasks.create_task(t, QUAL_SEED_BASE + 1)
        db.x("UPDATE tasks SET purpose='qualification', reward_cents=0 WHERE id=?", tid)


def qualified_templates(user_id: int) -> set[str]:
    return {r["template"] for r in db.q("SELECT DISTINCT template FROM qualifications WHERE user_id=? AND passed=1",
                                        user_id)}


def grade_qualification(session_id: int) -> dict:
    s = db.q1("SELECT * FROM sessions WHERE id=?", session_id)
    t = db.q1("SELECT * FROM tasks WHERE id=?", s["task_id"])
    if s["status"] == "submitted":
        raise quality.SubmitError("already submitted")
    report = quality.assess(session_id)
    v = report["verification"]
    passed = bool(v["passed"]) and report["process"]["coverage"] >= config.MIN_THOUGHT_COVERAGE
    with db.tx() as c:
        c.execute("INSERT INTO qualifications (user_id, template, session_id, score, passed, created_at)"
                  " VALUES (?,?,?,?,?,?)", (s["user_id"], t["template"], session_id, v["score"], int(passed), db.now()))
        c.execute("UPDATE sessions SET status='submitted', ended_at=? WHERE id=?", (db.now(), session_id))
    refresh_tier(s["user_id"])
    return {"template": t["template"], "passed": passed, "score": v["score"],
            "coverage": report["process"]["coverage"], "tips": report["tips"]}


def stats(user_id: int) -> dict:
    r = db.q1("SELECT SUM(status='approved') a, SUM(status='rejected') rj, AVG(CASE WHEN status='approved'"
              " THEN quality_score END) q FROM records WHERE user_id=? AND source='sandbox'", user_id)
    a, rj = r["a"] or 0, r["rj"] or 0
    return {"approved": a, "rejected": rj, "approval_rate": round(a / (a + rj), 3) if a + rj else None,
            "mean_quality": round(r["q"], 3) if r["q"] is not None else None,
            "qualified_templates": sorted(qualified_templates(user_id))}


def refresh_tier(user_id: int) -> str:
    st = stats(user_id)
    tier = "applicant"
    if st["qualified_templates"]:
        tier = "qualified"
        if (st["approved"] >= SENIOR_MIN_APPROVED and (st["mean_quality"] or 0) >= SENIOR_MIN_QUALITY
                and (st["approval_rate"] or 0) >= SENIOR_MIN_APPROVAL_RATE):
            tier = "senior"
    db.x("UPDATE users SET tier=? WHERE id=?", tier, user_id)
    return tier


def attempts(user_id: int, template: str) -> int:
    return db.q1("SELECT COUNT(*) n FROM qualifications WHERE user_id=? AND template=?", user_id, template)["n"]


def qualification_task_for(user_id: int, template: str) -> int:
    """Each attempt uses a different variant, so a failed attempt can't be memorized and retried."""
    from . import tasks
    tid = tasks.create_task(template, QUAL_SEED_BASE + 1 + attempts(user_id, template))
    db.x("UPDATE tasks SET purpose='qualification', reward_cents=0 WHERE id=?", tid)
    return tid


def can_start(user_id: int, task: dict) -> tuple[bool, str]:
    if task["purpose"] == "qualification":
        if task["template"] in qualified_templates(user_id):
            return False, "you already passed this qualification"
        if attempts(user_id, task["template"]) >= MAX_QUAL_ATTEMPTS:
            return False, f"no attempts left ({MAX_QUAL_ATTEMPTS} used); contact support"
        return True, ""
    if task["template"] not in qualified_templates(user_id):
        return False, "pass the qualification task for this task family first"
    return True, ""
