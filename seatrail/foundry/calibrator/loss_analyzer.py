"""Failure taxonomy for failed rollouts, and the buyer-facing Sample Benchmark & Loss Analysis report.

Buckets (first matching rule wins):
    ENVIRONMENT_ERROR            sandbox/model endpoint broke, or the grader reported an environment failure
    TOOL_LOOP_STALL              same call repeated 3+ times in a row, or ran out of steps / wall clock
    MISSING_DELIVERABLE_FILE     an expected deliverable was never written
    SCHEMA_OR_FORMAT_VIOLATION   deliverable exists but does not parse / breaks structure
    PARTIAL_DOC_READ_OMISSION    never opened an input a correct solution has to read
    HALLUCINATED_CITATION        final answer cites a file that does not exist in the workspace
    VERIFY_PRECISION_MISS        did the work, got details wrong (default)
"""
from __future__ import annotations

import html
import json
import re
from collections import Counter

from ... import db
from ...pipeline.pii import Redactor
from ..models import EnvSpec, FailureBucket, GraderResult
from ..sandbox_runner.runner import Trajectory

B = FailureBucket
CITE = re.compile(r"(?:/workspace/)?([\w./-]+\.(?:py|md|txt|json|csv|docx|pdf|xlsx|step|stp|png))\b")


def _repeats(traj: Trajectory, n: int = 3) -> bool:
    calls = [json.dumps([c["name"], c["args"]], sort_keys=True)
             for t in traj.turns if t["type"] == "assistant" for c in t["tool_calls"]]
    run = 1
    for a, b in zip(calls, calls[1:]):
        run = run + 1 if a == b else 1
        if run >= n:
            return True
    return False


def bucket_for(traj: Trajectory, g: GraderResult, spec: EnvSpec) -> str:
    if traj.stop_reason in ("env_error", "circuit_open") or g.failure_mode == "environment":
        return B.ENVIRONMENT_ERROR
    if _repeats(traj) or traj.stop_reason in ("max_steps", "wall_clock_timeout"):
        return B.TOOL_LOOP_STALL
    if g.failure_mode == "missing_deliverable":
        return B.MISSING_DELIVERABLE_FILE
    if g.failure_mode == "format":
        return B.SCHEMA_OR_FORMAT_VIOLATION
    if spec.required_reads and any(r not in traj.reads for r in spec.required_reads):
        return B.PARTIAL_DOC_READ_OMISSION
    known = set(traj.files)
    cited = {m.group(1).lstrip("./") for m in CITE.finditer(traj.final_text or "")}
    if any(c not in known and not any(k.endswith(c) for k in known) for c in cited):
        return B.HALLUCINATED_CITATION
    return B.VERIFY_PRECISION_MISS


def diagnose(rows: list[dict]) -> str:
    """One-line summary for the REWORK reason of a too-hard task."""
    counts = Counter(r["bucket"] for r in rows if r.get("bucket"))
    if not counts:
        return "no failure data"
    return "top failure modes: " + ", ".join(f"{b} x{n}" for b, n in counts.most_common(3))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def _anonymize(text: str, models: list[str]) -> str:
    for i, m in enumerate(sorted(models, key=len, reverse=True)):
        text = text.replace(m, f"Model {chr(65 + i)}")
    r = Redactor()
    return r.text(text)


def _snippet(trace_uri: str, models: list[str], max_turns: int = 6) -> str:
    try:
        lines = open(trace_uri).read().splitlines()[1:]
    except OSError:
        return ""
    out = []
    for ln in lines[-max_turns:]:
        t = json.loads(ln)
        if t["type"] == "assistant":
            calls = "; ".join(f"{c['name']}({json.dumps(c['args'])[:120]})" for c in t["tool_calls"])
            out.append(f"agent: {t['text'][:200]} {calls}".strip())
        elif t["type"] == "tool_result":
            out.append(f"tool[{t['name']}]{' ERROR' if t['is_error'] else ''}: {t['output'][:200]}")
        else:
            out.append(f"{t['type']}: {t.get('error', '')[:200]}")
    return _anonymize("\n".join(out), models)


def batch_report(batch_id: int, anonymize_models: bool = False) -> dict:
    """Latest calibration per task in the batch, rolled up for a buyer."""
    from .. import store
    batch = store.get_batch(batch_id)
    task_ids = store.batch_task_ids(batch_id)
    latest_groups = {}
    for tid in task_ids:
        r = db.q1("SELECT group_id FROM calibration_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", tid)
        if r:
            latest_groups[tid] = r["group_id"]
    runs = [r for tid, g in latest_groups.items()
            for r in db.q("SELECT * FROM calibration_runs WHERE task_id=? AND group_id=?", tid, g)]
    models = sorted({r["model_name"] for r in runs})
    alias = {m: (f"Model {chr(65 + i)}" if anonymize_models else m) for i, m in enumerate(models)}
    by_model = []
    for m in models:
        rs = [r for r in runs if r["model_name"] == m]
        by_model.append({"model": alias[m], "tasks": len(rs),
                         "pass_at_1": round(sum(r["pass_at_1"] for r in rs) / len(rs), 4),
                         "pass_at_k": round(sum(r["pass_at_k"] for r in rs) / len(rs), 4),
                         "pass_hat_k": round(sum(r["pass_hat_k"] for r in rs) / len(rs), 4),
                         "k": max(r["k_rollouts"] for r in rs),
                         "mean_score": round(sum(r["mean_score"] for r in rs) / len(rs), 4),
                         "avg_steps": round(sum(r["avg_steps"] for r in rs) / len(rs), 2),
                         "cost_usd": round(sum(r["token_cost_usd"] for r in rs), 4)})
    run_ids = [r["id"] for r in runs]
    rollouts = db.q(f"SELECT r.*, c.model_name, c.task_id FROM rollouts r JOIN calibration_runs c ON c.id=r.run_id"
                    f" WHERE r.run_id IN ({','.join('?' * len(run_ids))})", *run_ids) if run_ids else []
    failures = [r for r in rollouts if not r["passed"]]
    dist = Counter(r["failure_bucket"] or B.VERIFY_PRECISION_MISS for r in failures)
    distribution = [{"bucket": b.value, "count": dist.get(b.value, 0),
                     "share": round(dist.get(b.value, 0) / len(failures), 4) if failures else 0.0} for b in B]
    # 2-3 representative failures: the most common buckets first, one per bucket
    snippets = []
    for b, _ in dist.most_common(3):
        r = next(x for x in failures if (x["failure_bucket"] or B.VERIFY_PRECISION_MISS) == b)
        snippets.append({"bucket": b, "task_id": r["task_id"], "model": alias[r["model_name"]],
                         "score": r["score"], "excerpt": _snippet(r["trace_uri"], models)})
    sanity = []
    for tid in task_ids:
        s = db.q1("SELECT golden_score, empty_score, passed FROM sanity_checks WHERE task_id=? ORDER BY id DESC LIMIT 1",
                  tid)
        t = db.q1("SELECT title, status, verdict FROM foundry_tasks WHERE id=?", tid)
        sanity.append({"task_id": tid, "title": t["title"], "status": t["status"], "verdict": t["verdict"],
                       "golden": s["golden_score"] if s else None, "empty": s["empty_score"] if s else None,
                       "sanity_passed": bool(s and s["passed"])})
    verdicts = Counter(t["verdict"] for t in sanity if t["verdict"])
    return {"batch": {k: batch[k] for k in ("id", "client_name", "deal_type", "domain", "target_pass_rate_min",
                                            "target_pass_rate_max")},
            "tasks_calibrated": len(latest_groups), "tasks_total": len(task_ids),
            "verdicts": dict(verdicts), "by_model": by_model, "failure_distribution": distribution,
            "failure_snippets": snippets, "sanity": sanity}


def to_markdown(rep: dict) -> str:
    b = rep["batch"]
    lines = [f"# Sample Benchmark & Loss Analysis: {b['client_name']} / {b['domain']}", "",
             f"Batch {b['id']} ({b['deal_type']}). {rep['tasks_calibrated']} of {rep['tasks_total']} tasks calibrated. "
             f"Target band: {b['target_pass_rate_min']:.0%} to {b['target_pass_rate_max']:.0%} frontier pass rate.", "",
             "Verdicts: " + (", ".join(f"{k} {v}" for k, v in sorted(rep["verdicts"].items())) or "none yet"), "",
             "## Frontier model results", "",
             "| Model | Tasks | pass@1 | pass@k | pass^k | k | Mean score | Avg steps | Cost (USD) |",
             "|---|---|---|---|---|---|---|---|---|"]
    for m in rep["by_model"]:
        lines.append(f"| {m['model']} | {m['tasks']} | {m['pass_at_1']:.0%} | {m['pass_at_k']:.0%} | "
                     f"{m['pass_hat_k']:.0%} | {m['k']} | {m['mean_score']:.2f} | {m['avg_steps']} | {m['cost_usd']:.2f} |")
    lines += ["", "## Where models fail", "", "| Failure mode | Count | Share |", "|---|---|---|"]
    lines += [f"| {d['bucket']} | {d['count']} | {d['share']:.0%} |" for d in rep["failure_distribution"] if d["count"]]
    if rep["failure_snippets"]:
        lines += ["", "## Representative failures (anonymized)", ""]
        for s in rep["failure_snippets"]:
            lines += [f"**{s['bucket']}**, task {s['task_id']}, {s['model']}, score {s['score']:.2f}", "",
                      "```", s["excerpt"], "```", ""]
    lines += ["", "## Environment sanity proof", "",
              "Every task is checked in its pinned environment before calibration: the golden solution must score "
              "100% and an empty submission 0%.", "",
              "| Task | Title | Golden | Empty | Status | Verdict |", "|---|---|---|---|---|---|"]
    for s in rep["sanity"]:
        g = "n/a" if s["golden"] is None else f"{s['golden']:.0%}"
        e = "n/a" if s["empty"] is None else f"{s['empty']:.0%}"
        lines.append(f"| {s['task_id']} | {s['title']} | {g} | {e} | {s['status']} | {s['verdict'] or ''} |")
    return "\n".join(lines) + "\n"


def to_html(rep: dict) -> str:
    """Printable HTML (Save as PDF from the browser) with the failure chart drawn inline."""
    md = to_markdown(rep)
    rows = [d for d in rep["failure_distribution"] if d["count"]]
    peak = max((d["count"] for d in rows), default=1)
    bars = "".join(
        f'<div class="bar"><span class="lbl">{html.escape(d["bucket"])}</span>'
        f'<span class="fill" style="width:{100 * d["count"] / peak:.0f}%"></span><span class="n">{d["count"]}</span></div>'
        for d in rows)
    body = []
    in_code = in_table = False
    for ln in md.splitlines():
        if ln.startswith("```"):
            body.append("</pre>" if in_code else "<pre>")
            in_code = not in_code
            continue
        if in_code:
            body.append(html.escape(ln))
            continue
        if ln.startswith("|"):
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if set("".join(cells)) <= set("-"):
                continue
            if not in_table:
                body.append("<table>")
                in_table = True
                body.append("<tr>" + "".join(f"<th>{html.escape(c)}</th>" for c in cells) + "</tr>")
            else:
                body.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            body.append("</table>")
            in_table = False
        if ln.startswith("# "):
            body.append(f"<h1>{html.escape(ln[2:])}</h1>")
        elif ln.startswith("## "):
            body.append(f"<h2>{html.escape(ln[3:])}</h2>")
            if ln == "## Where models fail" and bars:
                body.append(f'<div class="chart">{bars}</div>')
        elif ln.strip():
            t = html.escape(ln)
            t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
            body.append(f"<p>{t}</p>")
    if in_table:
        body.append("</table>")
    css = ("body{font:14px/1.5 system-ui,sans-serif;max-width:960px;margin:32px auto;padding:0 16px;color:#1d2433}"
           "table{border-collapse:collapse;width:100%;margin:8px 0}th,td{border:1px solid #d5dae3;padding:4px 8px;"
           "text-align:left}th{background:#f2f4f8}pre{background:#f6f7f9;padding:12px;white-space:pre-wrap;font-size:12px}"
           ".bar{display:flex;align-items:center;gap:8px;margin:4px 0}.lbl{width:260px;font-size:12px}"
           ".fill{height:14px;background:#3b6fd8;border-radius:2px}.n{font-size:12px}")
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>Loss Analysis</title><style>{css}</style>"
            f"</head><body>{''.join(body)}</body></html>")
