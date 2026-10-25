"""Task registry: create task rows from templates."""
from .. import db
from .accounting import TEMPLATES

DOMAIN = "accounting"


def generate(template: str, seed: int) -> dict:
    if template not in TEMPLATES:
        raise KeyError(f"unknown template {template}")
    return TEMPLATES[template](seed)


def create_task(template: str, seed: int) -> int:
    existing = db.q1("SELECT id FROM tasks WHERE template=? AND seed=?", template, seed)
    if existing:
        return existing["id"]
    t = generate(template, seed)
    return db.x(
        "INSERT INTO tasks (template, domain, difficulty, seed, title, brief, initial_state, answer_key,"
        " reward_cents, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        template, DOMAIN, t["difficulty"], seed, t["title"], t["brief"], db.dumps(t["state"]),
        db.dumps(t["answer"]), t["reward_cents"], db.now())


def public_task(row: dict) -> dict:
    """Task as the expert sees it. Never includes the answer key."""
    out = {k: row[k] for k in ("id", "template", "domain", "difficulty", "title", "brief", "reward_cents")}
    out["purpose"] = row.get("purpose") or "work"
    return out
