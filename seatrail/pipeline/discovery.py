"""Process discovery from captured episodes.

Task-mining tools stop at a process map for automation consultants. We build the same map, then
tie it to training data: how much of each workflow is already covered by approved trajectories,
and where to collect next (frequent, slow workflows with little coverage).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from statistics import median

from .. import db

MAX_ACTIVITIES = 14


def _norm_label(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    s = re.sub(r"\b[A-Z]{2,5}-?\d{3,}\b", "#", s)   # INV-31877, PO-5120 -> #
    s = re.sub(r"\d[\d,.]*", "#", s)
    return s[:40]


def activity(e: dict) -> str | None:
    """Abstract one raw event into an activity name shared across episodes."""
    p = e["payload"]
    t = e["type"]
    target = p.get("target") or {}
    label = _norm_label(target.get("label") or target.get("text") or target.get("name") or "")
    if t == "navigate":
        url = p.get("url") or (p.get("page") or {}).get("url") or ""
        path = re.sub(r"^https?://[^/]+", "", url).split("?")[0]
        path = re.sub(r"/\d+", "/:id", path) or "/"
        return f"Open {path}"
    if t == "click":
        return f"Click {label}" if label else None
    if t == "input":
        return f"Fill {label}" if label else "Fill field"
    if t == "select":
        return f"Choose {label}" if label else "Choose option"
    if t == "submit":
        return "Submit form"
    if t == "key" and p.get("key") == "Enter":
        return None
    return None


def _episodes(org_id: int) -> list[dict]:
    sessions = db.q("SELECT id, state, status, started_at FROM sessions WHERE org_id=? AND source='capture'"
                    " AND status<>'discarded'", org_id)
    approved = {r["session_id"] for r in db.q("SELECT session_id FROM records WHERE org_id=? AND"
                                              " status='approved'", org_id)}
    out = []
    for s in sessions:
        events = db.q("SELECT ts, type, payload FROM events WHERE session_id=? AND deleted=0 ORDER BY seq", s["id"])
        acts = []
        for e in events:
            e["payload"] = db.loads(e["payload"])
            a = activity(e)
            if a and (not acts or acts[-1] != a):  # collapse repeats
                acts.append(a)
        if not acts:
            continue
        out.append({"id": s["id"], "workflow": (db.loads(s["state"]) or {}).get("workflow") or "unlabeled",
                    "activities": acts, "duration": events[-1]["ts"] if events else 0,
                    "approved": s["id"] in approved})
    return out


def process_map(episodes: list[dict]) -> dict:
    """Directly-follows graph with frequencies, plus variants."""
    nodes: Counter = Counter()
    edges: Counter = Counter()
    pos = defaultdict(list)
    starts: Counter = Counter()
    ends: Counter = Counter()
    for ep in episodes:
        acts = ep["activities"]
        for i, a in enumerate(acts):
            nodes[a] += 1
            pos[a].append(i / max(1, len(acts) - 1))
        for a, b in zip(acts, acts[1:]):
            edges[(a, b)] += 1
        starts[acts[0]] += 1
        ends[acts[-1]] += 1
    keep = {a for a, _ in nodes.most_common(MAX_ACTIVITIES)}
    node_list = sorted(keep, key=lambda a: (sum(pos[a]) / len(pos[a]), -nodes[a]))
    variants = Counter(tuple(ep["activities"]) for ep in episodes)
    return {
        "nodes": [{"id": a, "count": nodes[a], "start": starts[a], "end": ends[a]} for a in node_list],
        "edges": [{"from": a, "to": b, "count": n} for (a, b), n in edges.most_common()
                  if a in keep and b in keep and a != b],
        "variants": [{"activities": list(v), "count": n, "share": round(n / len(episodes), 3)}
                     for v, n in variants.most_common(5)],
        "episodes": len(episodes),
    }


def summarize(org_id: int, monthly_factor: float = 1.0) -> dict:
    """Per-workflow stats and a collection priority score."""
    eps = _episodes(org_id)
    by_wf = defaultdict(list)
    for e in eps:
        by_wf[e["workflow"]].append(e)
    rows = []
    for wf, items in by_wf.items():
        durs = [e["duration"] for e in items]
        variants = len({tuple(e["activities"]) for e in items})
        approved = sum(e["approved"] for e in items)
        med = median(durs) if durs else 0
        coverage = approved / len(items)
        # Time spent on this workflow per period, discounted by what we already have.
        minutes = len(items) * med / 60 * monthly_factor
        priority = round(minutes * (1 - min(1.0, approved / 20)) * (1 + min(variants, 10) / 10), 2)
        rows.append({"workflow": wf, "episodes": len(items), "approved_records": approved,
                     "coverage": round(coverage, 3), "median_duration_s": round(med, 1),
                     "median_steps": median(len(e["activities"]) for e in items), "variants": variants,
                     "minutes_observed": round(minutes, 1), "collection_priority": priority})
    rows.sort(key=lambda r: -r["collection_priority"])
    return {"workflows": rows, "episodes": len(eps)}


def workflow_map(org_id: int, workflow: str) -> dict:
    eps = [e for e in _episodes(org_id) if e["workflow"] == workflow]
    return process_map(eps) if eps else {"nodes": [], "edges": [], "variants": [], "episodes": 0}
