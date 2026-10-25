"""Enterprise edition: capture real work inside a company's own apps and export post-training data.

Runs inside the customer's network. Nothing leaves it unless the org enables model grading, and
then only to the endpoint the org chose (for example its own Bedrock or Vertex gateway).
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path
from urllib.parse import urlparse

from . import config, db
from .pipeline import convert

DEFAULT_POLICY = {
    "domain": "accounting",
    # The recorder only captures on these hosts. Everything else is ignored, on the client and here.
    "allowed_hosts": ["localhost", "127.0.0.1"],
    # Inputs matching these selectors are recorded as [MASKED], never their value.
    "mask_selectors": ["input[type=password]", "[data-sensitive]", "[autocomplete=cc-number]"],
    "capture_input_values": True,
    "sensitive_terms": [],           # customer names, code names... redacted everywhere
    "judge": "off",                  # off | auto | on.  Off by default: data stays in the building
    "judge_base_url": None,          # e.g. the company's own model gateway
    "retention_days": 180,           # raw events are purged after this; reviewed records stay
    "require_consent": True,
    "workflows": ["month_end_close", "bank_reconciliation", "ap_invoice_processing", "expense_audit",
                  "journal_entry", "vendor_onboarding"],
}


class PolicyError(PermissionError):
    pass


def get_policy(org_id: int) -> dict:
    org = db.q1("SELECT policy FROM orgs WHERE id=?", org_id)
    return {**DEFAULT_POLICY, **(db.loads(org["policy"]) or {})}


def update_policy(org_id: int, user_id: int, changes: dict) -> dict:
    allowed = set(DEFAULT_POLICY)
    bad = set(changes) - allowed
    if bad:
        raise ValueError(f"unknown policy keys: {sorted(bad)}")
    policy = {**get_policy(org_id), **changes}
    db.x("UPDATE orgs SET policy=? WHERE id=?", db.dumps(policy), org_id)
    db.audit(org_id, user_id, "policy.update", f"org:{org_id}", changes=changes)
    return policy


def give_consent(user: dict) -> str:
    token = user.get("capture_token") or secrets.token_urlsafe(24)
    db.x("UPDATE users SET consent_at=?, capture_token=? WHERE id=?", db.now(), token, user["id"])
    db.audit(user["org_id"], user["id"], "consent.given", f"user:{user['id']}")
    return token


def revoke_consent(user: dict) -> None:
    db.x("UPDATE users SET consent_at=NULL, capture_token=NULL WHERE id=?", user["id"])
    db.audit(user["org_id"], user["id"], "consent.revoked", f"user:{user['id']}")


def user_by_token(token: str) -> dict | None:
    if not token:
        return None
    return db.q1("SELECT * FROM users WHERE capture_token=? AND role='employee'", token)


def recorder_config(user: dict) -> dict:
    p = get_policy(user["org_id"])
    return {"employee": user["name"], "allowed_hosts": p["allowed_hosts"], "mask_selectors": p["mask_selectors"],
            "capture_input_values": p["capture_input_values"], "workflows": p["workflows"],
            "consented": bool(user["consent_at"])}


def start_capture(user: dict, goal: str = "", workflow: str = "") -> int:
    p = get_policy(user["org_id"])
    if p["require_consent"] and not user["consent_at"]:
        raise PolicyError("employee has not consented to capture")
    sid = db.x("INSERT INTO sessions (org_id, source, user_id, status, goal, state, started_at)"
               " VALUES (?, 'capture', ?, 'in_progress', ?, ?, ?)",
               user["org_id"], user["id"], goal, db.dumps({"workflow": workflow}), db.now())
    db.audit(user["org_id"], user["id"], "capture.start", f"session:{sid}")
    return sid


def _host_allowed(url: str, hosts: list[str]) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(h == a or h.endswith("." + a) for a in hosts)


ALLOWED_CAPTURE_TYPES = {"navigate", "click", "input", "select", "key", "submit", "think"}


def ingest(session: dict, user: dict, events: list[dict]) -> dict:
    """Append recorder events. Events from hosts outside the allowlist are dropped here too."""
    if session["user_id"] != user["id"] or session["source"] != "capture":
        raise PolicyError("not your capture session")
    if session["status"] not in ("in_progress", "checked"):
        raise PolicyError("session is closed")
    p = get_policy(user["org_id"])
    last = db.q1("SELECT MAX(seq) AS m FROM events WHERE session_id=?", session["id"])["m"] or 0
    accepted = dropped = 0
    with db.tx() as c:
        for e in events:
            t = e.get("type")
            payload = e.get("payload") or {}
            if t not in ALLOWED_CAPTURE_TYPES:
                dropped += 1
                continue
            page_url = (payload.get("page") or {}).get("url") or payload.get("url") or ""
            if t != "think" and not _host_allowed(page_url, p["allowed_hosts"]):
                dropped += 1
                continue
            if t == "input" and not p["capture_input_values"]:
                payload = {**payload, "value": "", "masked": True}
            last += 1
            c.execute("INSERT INTO events (session_id, seq, ts, type, payload) VALUES (?,?,?,?,?)",
                      (session["id"], last, float(e.get("ts", 0)), t, db.dumps(payload)))
            accepted += 1
    return {"accepted": accepted, "dropped": dropped}


def stop_capture(session: dict) -> None:
    db.x("UPDATE sessions SET ended_at=? WHERE id=? AND ended_at IS NULL", db.now(), session["id"])


def edit_capture(session: dict, user: dict, goal=None, workflow=None, outcome=None, delete_seqs=None) -> None:
    """Employee review: label the episode and remove steps they don't want kept."""
    if session["user_id"] != user["id"]:
        raise PolicyError("not your session")
    if session["status"] in ("submitted", "discarded"):
        raise PolicyError("session is closed")
    with db.tx() as c:
        if goal is not None:
            c.execute("UPDATE sessions SET goal=? WHERE id=?", (goal, session["id"]))
        if workflow is not None:
            state = db.loads(session["state"]) or {}
            state["workflow"] = workflow
            c.execute("UPDATE sessions SET state=? WHERE id=?", (db.dumps(state), session["id"]))
        if outcome is not None:
            if outcome not in ("success", "partial", "failed", ""):
                raise ValueError("outcome must be success, partial or failed")
            c.execute("UPDATE sessions SET outcome=? WHERE id=?", (outcome, session["id"]))
        for seq in delete_seqs or []:
            # Deleting erases the content for good; a tombstone keeps sequence numbers stable.
            c.execute("UPDATE events SET deleted=1, payload='{}' WHERE session_id=? AND seq=?", (session["id"], seq))
    if delete_seqs:
        db.audit(user["org_id"], user["id"], "capture.delete_steps", f"session:{session['id']}",
                 count=len(delete_seqs))


def discard_capture(session: dict, user: dict) -> None:
    if session["user_id"] != user["id"]:
        raise PolicyError("not your session")
    if session["status"] == "submitted":
        raise PolicyError("already submitted")
    with db.tx() as c:
        c.execute("DELETE FROM events WHERE session_id=?", (session["id"],))
        c.execute("UPDATE sessions SET status='discarded', last_report=NULL, goal='' WHERE id=?", (session["id"],))
    db.audit(user["org_id"], user["id"], "capture.discard", f"session:{session['id']}")


def purge_expired(org_id: int, user_id: int, now: float | None = None) -> dict:
    """Delete raw events past the retention window. Reviewed records are kept."""
    days = get_policy(org_id)["retention_days"]
    cutoff = (now or db.now()) - days * 86400
    ids = [r["id"] for r in db.q("SELECT id FROM sessions WHERE org_id=? AND started_at<?", org_id, cutoff)]
    with db.tx() as c:
        for sid in ids:
            c.execute("DELETE FROM events WHERE session_id=?", (sid,))
    db.audit(org_id, user_id, "retention.purge", f"org:{org_id}", sessions=len(ids))
    return {"sessions_purged": len(ids), "cutoff": cutoff}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
FORMATS = ("chat", "trajectory", "both")


def create_export(org_id: int, user_id: int, fmt: str = "both", workflow: str | None = None,
                  min_quality: float = 0.0, source: str | None = None) -> dict:
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {FORMATS}")
    sql = "SELECT * FROM records WHERE org_id=? AND status='approved' AND quality_score>=?"
    args: list = [org_id, min_quality]
    if workflow:
        sql += " AND template=?"
        args.append(workflow)
    if source:
        sql += " AND source=?"
        args.append(source)
    rows = db.q(sql + " ORDER BY id", *args)
    eid = db.x("INSERT INTO exports (org_id, requested_by, format, filters, record_ids, path, created_at)"
               " VALUES (?,?,?,?,?,?,?)", org_id, user_id, fmt,
               db.dumps({"workflow": workflow, "min_quality": min_quality, "source": source}),
               db.dumps([r["id"] for r in rows]), "", db.now())
    out = config.DATA_DIR / "exports" / f"org_{org_id}" / f"export_{eid}"
    out.mkdir(parents=True, exist_ok=True)
    records = [db.loads(r["record"]) for r in rows]
    files = write_files(out, records, fmt, title=f"Export {eid}")
    db.x("UPDATE exports SET path=? WHERE id=?", str(out), eid)
    db.audit(org_id, user_id, "export.create", f"export:{eid}", records=len(rows), format=fmt)
    return {"id": eid, "records": len(rows), "files": files, "path": str(out)}


def write_files(out: Path, records: list[dict], fmt: str, title: str) -> list[str]:
    files = []
    if fmt in ("chat", "both"):
        with open(out / "sft_chat.jsonl", "w") as f:
            for r in records:
                f.write(json.dumps(convert.to_chat(r), ensure_ascii=False) + "\n")
        files.append("sft_chat.jsonl")
    if fmt in ("trajectory", "both"):
        with open(out / "trajectories.jsonl", "w") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        files.append("trajectories.jsonl")
    envs = [e for e in (convert.to_rl_env(r) for r in records) if e]
    if envs:
        with open(out / "rl_environments.jsonl", "w") as f:
            for e in envs:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        files.append("rl_environments.jsonl")
    (out / "datacard.md").write_text(datacard(records, title))
    files.append("datacard.md")
    manifest = {"schema": convert.SCHEMA, "records": len(records), "files": files}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    files.append("manifest.json")
    return files


def datacard(records: list[dict], title: str) -> str:
    n = len(records)
    by_wf: dict[str, int] = {}
    steps = 0
    q = []
    for r in records:
        by_wf[r["workflow"]] = by_wf.get(r["workflow"], 0) + 1
        steps += len(r["steps"])
        if r.get("quality", {}).get("score") is not None:
            q.append(r["quality"]["score"])
    lines = [f"# {title}", "", f"- Records: {n}", f"- Total steps: {steps}",
             f"- Mean quality score: {sum(q) / len(q):.3f}" if q else "- Mean quality score: n/a",
             f"- Schema: {convert.SCHEMA}", "", "## Workflows", ""]
    lines += [f"- {k}: {v}" for k, v in sorted(by_wf.items())]
    lines += ["", "## Processing", "",
              "- Captured with the employee's consent, on allowlisted hosts only (enterprise captures).",
              "- Employees reviewed each episode and could delete steps before submitting.",
              "- PII redacted with rule-based filters plus the organization's sensitive-term list.",
              "- Near-duplicates removed with SimHash; every record approved by a human reviewer.",
              "", "## Files", "",
              "- `sft_chat.jsonl`: agent SFT examples (system, user task, assistant reasoning + tool calls, tool results).",
              "- `trajectories.jsonl`: full step-level records with observations, outcome and quality scores.",
              "- `rl_environments.jsonl`: sandbox tasks only; initial state, tools and verifier reference."]
    return "\n".join(lines) + "\n"
