"""Quality pipeline shared by both editions.

assess(session)  -> report shown to the expert before they submit (nothing is stored as a record)
submit(session)  -> stores the training record and puts it in the human review queue
"""
from __future__ import annotations

from .. import config, db, verifier
from ..tasks import public_task
from . import convert, dedup, judge, pii

BLOCKING_FLAGS = {"verification_failed", "near_duplicate", "residual_pii", "low_reasoning_coverage", "too_short"}
CAPTURE_MIN_QUALITY = 0.5


class SubmitError(ValueError):
    pass


def _load(session_id: int):
    s = db.q1("SELECT * FROM sessions WHERE id=?", session_id)
    if not s:
        raise KeyError("session not found")
    s["state"] = db.loads(s["state"])
    events = db.q("SELECT seq, ts, type, payload, deleted FROM events WHERE session_id=? ORDER BY seq", session_id)
    for e in events:
        e["payload"] = db.loads(e["payload"])
    org = db.q1("SELECT * FROM orgs WHERE id=?", s["org_id"])
    org["policy"] = db.loads(org["policy"]) or {}
    task = None
    if s["task_id"]:
        task = db.q1("SELECT * FROM tasks WHERE id=?", s["task_id"])
        task["initial_state"] = db.loads(task["initial_state"])
        task["answer_key"] = db.loads(task["answer_key"])
    return s, events, org, task


def _process_metrics(record: dict, source: str) -> dict:
    n_explained, n_decisions = convert.decision_coverage(record)
    coverage = n_explained / n_decisions if n_decisions else 0.0
    words = len(convert.thought_text(record).split())
    duration = record["provenance"]["duration_s"]
    cov_part = min(1.0, coverage / 0.8)
    words_part = min(1.0, words / max(30, 4 * n_decisions))
    pace_part = 1.0 if duration >= 2 * max(1, n_decisions) else 0.5
    score = 0.6 * cov_part + 0.25 * words_part + 0.15 * pace_part
    return {"decisions": n_decisions, "explained": n_explained, "coverage": round(coverage, 3),
            "narration_words": words, "duration_s": duration, "score": round(score, 4)}


def build_record(session_id: int) -> tuple[dict, dict]:
    """Returns (record, context) without grading. Used by assess and by tests."""
    s, events, org, task = _load(session_id)
    red = pii.Redactor(org["policy"].get("sensitive_terms", []))
    verification = None
    if s["source"] == "sandbox":
        state = s["state"]
        verification = verifier.verify(state, task["answer_key"], config.VERIFY_PASS_SCORE)
        record = convert.sandbox_record(s, task, events, verification, red)
    else:
        meta = s["state"] or {}
        record = convert.capture_record(s, events, red, workflow=meta.get("workflow") or "unlabeled",
                                        domain=org["policy"].get("domain", "accounting"))
    return record, {"session": s, "org": org, "task": task, "verification": verification,
                    "pii": red.report(), "n_events": len(events),
                    "n_deleted": sum(1 for e in events if e["deleted"])}


def assess(session_id: int, use_cache: bool = True) -> dict:
    record, ctx = build_record(session_id)
    s, org, verification = ctx["session"], ctx["org"], ctx["verification"]
    cache_key = f"{ctx['n_events']}:{ctx['n_deleted']}:{s['goal']}:{s['outcome']}"
    if use_cache and s["last_report"]:
        prev = db.loads(s["last_report"])
        if prev.get("cache_key") == cache_key:
            return prev

    process = _process_metrics(record, s["source"])
    flags: list[str] = []

    # Duplicates: compare narration + actions against this org's existing records.
    # Narration only: experts solving the same kind of task will legitimately take similar actions,
    # but two traces with near-identical narration are a copy.
    sig = dedup.signature(convert.thought_text(record))
    dupes = []
    if len(convert.thought_text(record).split()) >= 20:
        others = [(r["id"], r["fingerprint"]) for r in db.q(
            "SELECT id, fingerprint FROM records WHERE org_id=? AND session_id<>?", s["org_id"], s["id"])]
        dupes = dedup.find_near_duplicates(sig, others, config.NEAR_DUP_JACCARD)
    if dupes:
        flags.append("near_duplicate")

    # Anything that still looks like PII after redaction blocks submission.
    import json
    residual = pii.residual_scan(json.dumps(record["steps"], ensure_ascii=False))
    if residual:
        flags.append("residual_pii")

    if process["decisions"] < 3:
        flags.append("too_short")
    if process["coverage"] < config.MIN_THOUGHT_COVERAGE:
        flags.append("low_reasoning_coverage")
    if verification is not None and not verification["passed"]:
        flags.append("verification_failed")

    grade = judge.judge(record, verification, org["policy"])

    if s["source"] == "sandbox":
        composite = 0.45 * verification["score"] + 0.35 * grade["score"] + 0.20 * process["score"]
        threshold = config.SUBMIT_MIN_QUALITY
    else:
        outcome_score = {"success": 1.0, "partial": 0.5, "failed": 0.0}.get(s["outcome"] or "", 0.3)
        composite = 0.25 * outcome_score + 0.50 * grade["score"] + 0.25 * process["score"]
        threshold = CAPTURE_MIN_QUALITY
        if not (s["goal"] or "").strip():
            flags.append("missing_goal")

    blocking = [f for f in flags if f in BLOCKING_FLAGS or f == "missing_goal"]
    if s["source"] == "capture":
        # Employees repeat routine workflows, so similar narration is expected. Show it to the steward
        # instead of blocking. In the marketplace, where people are paid per task, it blocks.
        blocking = [f for f in blocking if f != "near_duplicate"]
    can_submit = composite >= threshold and not blocking
    tips = []
    if verification is not None:
        tips += verifier.feedback_for_expert(verification)
    if "low_reasoning_coverage" in flags:
        tips.append(f"Explain more of your decisions out loud (now {process['coverage']:.0%}, need "
                    f"{config.MIN_THOUGHT_COVERAGE:.0%}).")
    if "near_duplicate" in flags:
        tips.append("This trace is nearly identical to an existing one." if s["source"] == "sandbox"
                    else "Very similar to a recording already submitted; the reviewer may skip it.")
    if "residual_pii" in flags:
        tips.append(f"Possible personal data remains: {', '.join(residual)}. Remove those steps or rephrase.")
    if "missing_goal" in flags:
        tips.append("Describe what you were trying to accomplish.")
    if "too_short" in flags:
        tips.append("The episode has fewer than 3 actions.")
    if grade.get("feedback_to_expert"):
        tips.append(grade["feedback_to_expert"])

    report = {
        "cache_key": cache_key,
        "session_id": s["id"],
        "source": s["source"],
        "quality_score": round(composite, 4),
        "threshold": threshold,
        "can_submit": can_submit,
        "flags": flags + [f for f in grade.get("flags", []) if f not in flags],
        "verification": verification,
        "judge": grade,
        "process": process,
        "pii": ctx["pii"],
        "residual_pii": residual,
        "duplicates": dupes,
        "fingerprint": dedup.encode(sig),
        "tips": tips,
        "preview": {"steps": len(record["steps"]), "first_steps": record["steps"][:3]},
    }
    with db.tx() as c:
        c.execute("UPDATE sessions SET last_report=?, status=CASE WHEN status='in_progress' THEN 'checked'"
                  " ELSE status END WHERE id=?", (db.dumps(report), s["id"]))
    return report


def submit(session_id: int) -> int:
    s = db.q1("SELECT * FROM sessions WHERE id=?", session_id)
    if not s:
        raise KeyError("session not found")
    if s["status"] == "submitted":
        raise SubmitError("already submitted")
    report = assess(session_id)
    if not report["can_submit"]:
        raise SubmitError("quality check not passed: " + "; ".join(report["tips"][:3]))
    record, ctx = build_record(session_id)
    record["quality"] = {"score": report["quality_score"], "judge": report["judge"], "process": report["process"],
                         "flags": report["flags"]}
    task = ctx["task"]
    template = task["template"] if task else (ctx["session"]["state"] or {}).get("workflow") or "unlabeled"
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO records (org_id, source, session_id, task_id, user_id, template, record, verification,"
            " judge, pii, fingerprint, flags, quality_score, status, created_at) VALUES"
            " (?,?,?,?,?,?,?,?,?,?,?,?,?, 'needs_review', ?)",
            (s["org_id"], s["source"], s["id"], s["task_id"], s["user_id"], template, db.dumps(record),
             db.dumps(report["verification"]), db.dumps(report["judge"]), db.dumps(report["pii"]),
             report["fingerprint"], db.dumps(report["flags"]), report["quality_score"], db.now()))
        c.execute("UPDATE sessions SET status='submitted', ended_at=COALESCE(ended_at, ?) WHERE id=?",
                  (db.now(), s["id"]))
        rid = cur.lastrowid
    db.audit(s["org_id"], s["user_id"], "record.submit", f"record:{rid}", quality=report["quality_score"])
    return rid


def review(record_id: int, reviewer_id: int, approve: bool, notes: str = "") -> dict:
    r = db.q1("SELECT * FROM records WHERE id=?", record_id)
    if not r:
        raise KeyError("record not found")
    if r["status"] != "needs_review":
        raise SubmitError(f"record is already {r['status']}")
    status = "approved" if approve else "rejected"
    with db.tx() as c:
        c.execute("UPDATE records SET status=?, reviewer_id=?, review_notes=?, reviewed_at=? WHERE id=?",
                  (status, reviewer_id, notes, db.now(), record_id))
        # Marketplace experts earn the task reward when their record is approved.
        if approve and r["source"] == "sandbox" and r["task_id"]:
            reward = c.execute("SELECT reward_cents FROM tasks WHERE id=?", (r["task_id"],)).fetchone()[0]
            c.execute("INSERT INTO payouts (expert_id, kind, amount_cents, record_id, created_at)"
                      " VALUES (?, 'task_reward', ?, ?, ?)", (r["user_id"], reward, record_id, db.now()))
    db.audit(r["org_id"], reviewer_id, f"record.{status}", f"record:{record_id}", notes=notes)
    return {"id": record_id, "status": status}


def task_summary(task_row: dict) -> dict:
    return public_task(task_row)
