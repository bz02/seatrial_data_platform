"""Programmatic verification of a sandbox task's final state against its hidden answer key."""
from __future__ import annotations


def _norm(v) -> str:
    return "" if v is None else str(v).strip().lower()


def verify(state: dict, answer: dict, pass_score: float) -> dict:
    checks = []
    sheets = {s["name"]: s for s in state["sheets"]}

    # Cells
    total = correct = 0
    wrong = []
    for sheet_name, rows in answer.get("cells", {}).items():
        by_id = {r["id"]: r for r in sheets[sheet_name]["rows"]}
        for row_id, cols in rows.items():
            for col, expected in cols.items():
                total += 1
                got = by_id.get(row_id, {}).get(col)
                if _norm(got) == _norm(expected):
                    correct += 1
                else:
                    wrong.append({"sheet": sheet_name, "row": row_id, "column": col, "got": got or ""})
    cell_score = correct / total if total else 1.0
    checks.append({"name": "cells", "score": round(cell_score, 4), "detail": f"{correct}/{total} cells correct",
                   "wrong": wrong[:50]})

    # Fields
    fields = {f["key"]: f.get("value") for f in state["fields"]}
    f_total = len(answer.get("fields", {}))
    f_ok, f_wrong = 0, []
    for k, expected in answer.get("fields", {}).items():
        got = fields.get(k)
        if got is not None and abs(float(got) - float(expected)) <= 0.01:
            f_ok += 1
        else:
            f_wrong.append({"field": k, "got": got})
    field_score = f_ok / f_total if f_total else 1.0
    checks.append({"name": "fields", "score": round(field_score, 4), "detail": f"{f_ok}/{f_total} fields correct",
                   "wrong": f_wrong})

    # Emails
    reqs = answer.get("emails", [])
    e_ok, e_detail = 0, []
    for req in reqs:
        hit = False
        for m in state.get("outbox", []):
            if _norm(m["to"]) != _norm(req["to"]):
                continue
            text = (m.get("subject", "") + " " + m.get("body", "")).lower()
            if all(any(term.lower() in text for term in group) for group in req.get("must_mention_any", [])):
                hit = True
                break
        e_ok += hit
        e_detail.append({"to": req["to"], "ok": hit})
    email_score = e_ok / len(reqs) if reqs else 1.0
    checks.append({"name": "emails", "score": round(email_score, 4), "detail": f"{e_ok}/{len(reqs)} required emails",
                   "wrong": [d for d in e_detail if not d["ok"]]})

    w = answer.get("weights", {"cells": 0.6, "fields": 0.3, "emails": 0.1})
    wsum = sum(w.values()) or 1
    score = (w.get("cells", 0) * cell_score + w.get("fields", 0) * field_score + w.get("emails", 0) * email_score) / wsum
    return {"score": round(score, 4), "passed": score >= pass_score and bool(state.get("submitted")),
            "submitted": bool(state.get("submitted")), "checks": checks}


def feedback_for_expert(v: dict) -> list[str]:
    """Hints that point at problems without leaking the answer key."""
    tips = []
    for c in v["checks"]:
        if c["score"] >= 1:
            continue
        if c["name"] == "cells":
            sheets = sorted({w["sheet"] for w in c["wrong"]})
            tips.append(f"{c['detail']}. Recheck: {', '.join(sheets)}.")
        elif c["name"] == "fields":
            tips.append(f"{c['detail']}. Recheck: {', '.join(w['field'] for w in c['wrong'])}.")
        elif c["name"] == "emails":
            tips.append(f"{c['detail']}. A required email is missing or incomplete.")
    if not v["submitted"]:
        tips.append("Press Finish task when you are done.")
    return tips
