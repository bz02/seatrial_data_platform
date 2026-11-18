"""Foundry HTTP API, versioned under /api/v1. Platform admins only for now; SME roles come with
the dispatch module (P4)."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from .. import config
from . import jobs, store
from .calibrator import calibrate, loss_analyzer
from .calibrator.adapters import default_panel, get_adapter
from .models import (AssignmentIn, CalibrationRunIn, ContractBatchIn, ExpertProfileIn, LeakageStatus, PiiScanStatus,
                     QaDecisionIn, RubricCriterionIn, SourceAssetIn, SynthRunIn, TaskIn, TransitionIn,
                     ClaimDecisionIn, WarrantyClaimIn)
from .sandbox_runner.runner import run_env_sanity_check
from .state_machine import TransitionError

router = APIRouter(prefix="/api/v1", tags=["foundry"])


def _admin(user: dict) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "requires role: admin")
    return user


def admin_dep():
    from ..app import current_user  # late import: app.py includes this router

    def dep(user=Depends(current_user)):
        return _admin(user)
    return dep


ADMIN = Depends(admin_dep())


def conflict(e: TransitionError):
    raise HTTPException(409, {"code": e.code, "message": str(e)})


@router.post("/batches")
def create_batch(body: ContractBatchIn, user=ADMIN):
    return store.create_batch(body)


@router.get("/batches/{bid}")
def get_batch(bid: int, user=ADMIN):
    b = store.get_batch(bid)
    b["task_ids"] = store.batch_task_ids(bid)
    return b


@router.post("/source-assets")
def create_source_asset(body: SourceAssetIn, user=ADMIN):
    return store.create_source_asset(body)


@router.post("/source-assets/{aid}/scan")
def set_scan(aid: int, body: dict = Body(...), user=ADMIN):
    pii = PiiScanStatus(body["pii_scan_status"]) if body.get("pii_scan_status") else None
    leak = LeakageStatus(body["outcome_leakage_status"]) if body.get("outcome_leakage_status") else None
    return store.update_source_scan(aid, pii, leak)


@router.post("/tasks")
def create_task(body: TaskIn, user=ADMIN):
    return store.create_task(body)


@router.post("/tasks/import-sheet")
def import_sheet(body: dict = Body(...), user=ADMIN):
    return store.import_sheet_task(int(body["batch_id"]), int(body["sheet_task_id"]), body.get("author_id"))


@router.get("/tasks/{tid}")
def get_task(tid: int, user=ADMIN):
    t = store.get_task(tid)
    t["cost"] = store.task_cost(tid)
    return t


@router.patch("/tasks/{tid}")
def edit_task(tid: int, body: dict = Body(...), user=ADMIN):
    try:
        return store.update_task_spec(tid, body.get("env"), body.get("sealed"), user["id"])
    except TransitionError as e:
        conflict(e)


@router.post("/tasks/{tid}/transition")
def transition(tid: int, body: TransitionIn, user=ADMIN):
    try:
        return store.transition(tid, body.to, user["id"], body.reason)
    except TransitionError as e:
        conflict(e)


@router.post("/tasks/{tid}/criteria")
def add_criterion(tid: int, body: RubricCriterionIn, user=ADMIN):
    return store.add_criterion(tid, body, user["id"])


@router.post("/tasks/{tid}/assignments")
def assign(tid: int, body: AssignmentIn, user=ADMIN):
    try:
        return store.assign(tid, body)
    except TransitionError as e:
        conflict(e)


@router.post("/tasks/{tid}/sanity-check")
def sanity_check(tid: int, user=ADMIN):
    return run_env_sanity_check(tid)


@router.post("/calibration/run")
def calibration_run(body: CalibrationRunIn, sync: bool = Query(False), user=ADMIN):
    models = body.models or default_panel()
    for m in models:  # fail fast on unknown models / missing keys, before queuing anything
        get_adapter(m)
    if body.task_id is not None:
        store.get_task(body.task_id)

        def fn():
            return calibrate.calibrate_task(body.task_id, models=models, k=body.k, concurrency=body.concurrency,
                                            actor_id=user["id"])
    else:
        store.get_batch(body.batch_id)

        def fn():
            return calibrate.calibrate_batch(body.batch_id, models=models, k=body.k, concurrency=body.concurrency,
                                             actor_id=user["id"])
    return jobs.submit("calibration", {**body.model_dump(), "models": models}, fn, sync=sync or config.FOUNDRY_SYNC_JOBS)


@router.get("/jobs/{jid}")
def get_job(jid: int, user=ADMIN):
    return jobs.get(jid)


@router.get("/calibration/batch/{bid}/report")
def report(bid: int, format: str = Query("json", pattern="^(json|md|html)$"), anonymize: bool = Query(False),
           user=ADMIN):
    rep = loss_analyzer.batch_report(bid, anonymize_models=anonymize)
    if format == "md":
        return PlainTextResponse(loss_analyzer.to_markdown(rep), media_type="text/markdown")
    if format == "html":
        return HTMLResponse(loss_analyzer.to_html(rep))
    return rep


# ---------------------------------------------------------------------------
# P1 ingestion and compliance gate
# ---------------------------------------------------------------------------
def _inside_ingest_root(path: str) -> str:
    import os
    from pathlib import Path
    root = Path(os.environ.get("SEATRAIL_INGEST_ROOT", str(config.DATA_DIR / "inbox"))).resolve()
    p = Path(path).resolve()
    if p != root and root not in p.parents:
        raise HTTPException(403, f"server-side paths must be under SEATRAIL_INGEST_ROOT ({root})")
    return str(p)


@router.post("/ingestion/scan")
def ingestion_scan(body: dict = Body(...), user=ADMIN):
    from .ingestion import service
    folder, git_url = body.get("folder"), body.get("git_url")
    if folder:
        folder = _inside_ingest_root(folder)
    if git_url and not git_url.startswith("https://"):
        git_url = _inside_ingest_root(git_url)
    asset = SourceAssetIn(**{k: body[k] for k in SourceAssetIn.model_fields if k in body})
    out = service.ingest(asset, folder=folder, git_url=git_url, base_commit=body.get("base_commit"),
                         client_names=body.get("client_names") or [], domain_tag=body.get("domain_tag", ""),
                         acquisition_cost_usd=float(body.get("acquisition_cost_usd", 0)))
    return {k: v for k, v in out.items() if k != "manifest"} | {"files": len(out["manifest"])}


@router.get("/ingestion/{aid}/alerts")
def ingestion_alerts(aid: int, user=ADMIN):
    from .ingestion import service
    return service.alerts(aid)


@router.post("/ingestion/{aid}/approve")
def ingestion_approve(aid: int, user=ADMIN):
    from .ingestion import compliance_gate
    try:
        return compliance_gate.approve(aid, user["id"])
    except TransitionError as e:
        conflict(e)


@router.post("/ingestion/alerts/{alert_id}/resolve")
def ingestion_resolve(alert_id: int, body: dict = Body(...), user=ADMIN):
    from .ingestion import compliance_gate
    return compliance_gate.resolve(alert_id, user["id"], body.get("note", ""))


@router.post("/ingestion/alerts/{alert_id}/waive")
def ingestion_waive(alert_id: int, body: dict = Body(...), user=ADMIN):
    from .ingestion import compliance_gate
    return compliance_gate.waive(alert_id, user["id"], body.get("reason", ""))


# ---------------------------------------------------------------------------
# P4 dispatch, workbench and QA (experts with a profile, and admins)
# ---------------------------------------------------------------------------
def expert_dep():
    from ..app import current_user

    def dep(user=Depends(current_user)):
        if user["role"] not in ("expert", "admin"):
            raise HTTPException(403, "requires role: expert or admin")
        return user
    return dep


EXPERT = Depends(expert_dep())


def _task_access(tid: int, user: dict, need: str = "view") -> dict:
    from .. import db
    t = store.get_task(tid)
    rev = db.q1("SELECT reviewer_id FROM sme_assignments WHERE task_id=? AND active=1 ORDER BY id DESC LIMIT 1", tid)
    is_author = t["author_id"] == user["id"]
    is_reviewer = bool(rev and rev["reviewer_id"] == user["id"])
    if user["role"] == "admin":
        return t
    if need == "author" and not is_author:
        raise HTTPException(403, "only the task's author can do this")
    if need == "view" and not (is_author or is_reviewer):
        raise HTTPException(403, "you are not the author or the assigned reviewer of this task")
    return t


@router.put("/experts/{uid}/profile")
def put_profile(uid: int, body: ExpertProfileIn, user=ADMIN):
    from .dispatch import service as dispatch
    return dispatch.upsert_profile(uid, body)


@router.post("/dispatch/auto")
def auto_dispatch(user=ADMIN):
    from .dispatch import service as dispatch
    return dispatch.auto_dispatch_reviews()


@router.get("/tasks/{tid}/audit")
def task_audit(tid: int, user=ADMIN):
    return store.audit_events(tid)


@router.get("/workbench/queue")
def wb_queue(user=EXPERT):
    from .dispatch import service as dispatch
    return dispatch.queue(user["id"])


@router.post("/workbench/seeds/{aid}/claim")
def wb_claim_seed(aid: int, user=EXPERT):
    from .dispatch import service as dispatch
    try:
        return dispatch.claim_seed(user["id"], aid)
    except TransitionError as e:
        conflict(e)


def _seed_access(aid: int, user: dict):
    from .. import db
    c = db.q1("SELECT user_id FROM seed_claims WHERE asset_id=?", aid)
    if user["role"] != "admin" and (not c or c["user_id"] != user["id"]):
        raise HTTPException(403, "claim this seed first")


@router.get("/workbench/seeds/{aid}/files")
def wb_seed_files(aid: int, path: str | None = None, user=EXPERT):
    import base64
    from .ingestion.service import clean_dir
    _seed_access(aid, user)
    root = clean_dir(aid).resolve()
    if path is None:
        return [str(p.relative_to(root)) for p in sorted(root.rglob("*")) if p.is_file()]
    p = (root / path).resolve()
    if root not in p.parents or not p.is_file():
        raise HTTPException(404, "no such file")
    data = p.read_bytes()
    try:
        return {"path": path, "text": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"path": path, "base64": base64.b64encode(data).decode()}


@router.post("/workbench/tasks")
def wb_create_task(body: TaskIn, user=EXPERT):
    from .. import db
    from .dispatch import service as dispatch
    body = body.model_copy(update={"author_id": user["id"], "origin": "HUMAN", "parent_task_id": None})
    for aid in body.source_asset_ids:
        c = db.q1("SELECT user_id FROM seed_claims WHERE asset_id=?", aid)
        if user["role"] != "admin" and (not c or c["user_id"] != user["id"]):
            raise HTTPException(403, f"seed {aid} is not claimed by you")
    t = store.create_task(body)
    for aid in body.source_asset_ids:
        dispatch.link_seed(aid, t["id"])
    return t


@router.get("/workbench/tasks/{tid}")
def wb_task(tid: int, user=EXPERT):
    from .. import db
    from .dispatch import service as dispatch
    t = _task_access(tid, user)
    t["sealed"] = store.task_sealed(tid)  # the author wrote it; the reviewer must check the verifier
    t["authoring"] = dispatch.authoring_check(tid)
    last = db.q1("SELECT group_id FROM calibration_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", tid)
    t["rollouts"] = db.q("SELECT r.id, c.model_name, r.k_index, r.score, r.passed, r.steps, r.stop_reason,"
                         " r.failure_bucket FROM rollouts r JOIN calibration_runs c ON c.id=r.run_id WHERE c.task_id=?"
                         " AND c.group_id=? ORDER BY c.id, r.k_index", tid, last["group_id"]) if last else []
    t["qa_reviews"] = db.q("SELECT * FROM qa_reviews WHERE task_id=? ORDER BY id", tid)
    return t


@router.patch("/workbench/tasks/{tid}")
def wb_edit(tid: int, body: dict = Body(...), user=EXPERT):
    _task_access(tid, user, "author")
    try:
        return store.update_task_spec(tid, body.get("env"), body.get("sealed"), user["id"])
    except TransitionError as e:
        conflict(e)


@router.post("/workbench/tasks/{tid}/criteria")
def wb_criterion(tid: int, body: RubricCriterionIn, user=EXPERT):
    t = _task_access(tid, user, "author")
    if t["status"] not in ("DRAFT", "REWORK"):
        raise HTTPException(409, {"code": "not_editable", "message": f"task is {t['status']}"})
    return store.add_criterion(tid, body, user["id"])


@router.post("/workbench/tasks/{tid}/submit")
def wb_submit(tid: int, user=EXPERT):
    t = _task_access(tid, user, "author")
    if t["status"] not in ("DRAFT", "REWORK"):
        raise HTTPException(409, {"code": "not_editable", "message": f"task is {t['status']}"})
    sanity = run_env_sanity_check(tid)
    if not sanity["passed"]:
        return {"submitted": False, "sanity": sanity}
    try:
        return {"submitted": True, "sanity": sanity,
                "task": store.transition(tid, "SYNTH_CHECK", user["id"], "submitted by author")}
    except TransitionError as e:
        conflict(e)


@router.post("/workbench/tasks/{tid}/claim-review")
def wb_claim_review(tid: int, user=EXPERT):
    from .dispatch import service as dispatch
    try:
        return dispatch.claim_review(user["id"], tid)
    except TransitionError as e:
        conflict(e)


@router.post("/workbench/tasks/{tid}/qa")
def wb_qa(tid: int, body: QaDecisionIn, user=EXPERT):
    from .dispatch import service as dispatch
    try:
        return dispatch.qa_decide(tid, user["id"], body)
    except TransitionError as e:
        conflict(e)


@router.get("/workbench/rollouts/{rid}")
def wb_rollout(rid: int, user=EXPERT):
    import json as _json
    from .. import db
    r = db.q1("SELECT r.*, c.task_id, c.model_name FROM rollouts r JOIN calibration_runs c ON c.id=r.run_id"
              " WHERE r.id=?", rid)
    if not r:
        raise HTTPException(404, "rollout not found")
    _task_access(r["task_id"], user)
    try:
        lines = [_json.loads(x) for x in open(r["trace_uri"]).read().splitlines()]
    except OSError:
        lines = []
    return {"rollout": {k: r[k] for k in ("id", "task_id", "model_name", "score", "passed", "steps", "stop_reason",
                                          "failure_bucket")}, "summary": lines[0] if lines else {},
            "turns": lines[1:]}


@router.post("/workbench/lint")
def wb_lint(body: dict = Body(...), user=EXPERT):
    from .dispatch.rules import criterion_problems, lint_prompt
    out = {"prompt": lint_prompt(body.get("instructions", ""))}
    if "criterion" in body:
        c = body["criterion"]
        out["criterion"] = criterion_problems(c.get("kind", ""), c.get("spec_json") or {},
                                              c.get("ground_truth_evidence_ref", ""))
    return out


# ---------------------------------------------------------------------------
# P5: hybrid synthesis
# ---------------------------------------------------------------------------
@router.post("/synth/runs")
def synth_run(body: SynthRunIn, sync: bool = Query(False), user=ADMIN):
    from .synthesizer import llm, pipeline
    d_model, d_critic = llm.default_models()
    m, c = llm.get_text_model(body.model or d_model), llm.get_text_model(body.critic_model or d_critic)
    store.get_batch(body.batch_id)
    store.get_source_asset(body.asset_id)

    def fn():
        return pipeline.synthesize(body.asset_id, body.batch_id, m, c, max_candidates=body.max_candidates,
                                   archetype=body.archetype, author_id=body.author_id,
                                   max_revisions=body.max_revisions)
    return jobs.submit("synthesis", body.model_dump(), fn, sync=sync or config.FOUNDRY_SYNC_JOBS)


@router.get("/synth/runs")
def synth_runs(asset_id: int | None = None, group_id: str | None = None, user=ADMIN):
    from .synthesizer import pipeline
    return pipeline.runs(asset_id, group_id)


# ---------------------------------------------------------------------------
# P6: packaging and delivery
# ---------------------------------------------------------------------------
@router.post("/batches/{bid}/releases")
def build_release(bid: int, body: dict = Body(default={}), user=ADMIN):
    from .delivery import packager
    try:
        return packager.package_batch(bid, body.get("task_ids"), body.get("include_trajectories"), user["id"],
                                      body.get("client_names"))
    except TransitionError as e:
        conflict(e)


@router.get("/releases/{rid}")
def release(rid: int, user=ADMIN):
    from .delivery import gateway, packager
    return {**packager.get_release(rid), "downloads": gateway.downloads(rid)}


@router.post("/releases/{rid}/verify")
def release_verify(rid: int, user=ADMIN):
    from .delivery import packager
    return packager.verify_release(rid)


@router.post("/releases/{rid}/deliver")
def release_deliver(rid: int, body: dict = Body(default={}), user=ADMIN):
    from .delivery import gateway
    try:
        return gateway.deliver(rid, user["id"], float(body.get("ttl_days", 7)))
    except TransitionError as e:
        conflict(e)


@router.post("/releases/{rid}/link")
def release_link(rid: int, body: dict = Body(default={}), user=ADMIN):
    from .delivery import gateway
    try:
        return gateway.link(rid, float(body.get("ttl_days", 7)))
    except TransitionError as e:
        conflict(e)


@router.post("/releases/{rid}/revoke")
def release_revoke(rid: int, body: dict = Body(default={}), user=ADMIN):
    from .delivery import gateway
    return gateway.revoke(rid, user["id"], body.get("reason", ""))


@router.get("/deliveries/{token}")
def delivery_download(token: str, request: Request):
    """Public: the signed link is the credential."""
    from fastapi.responses import FileResponse
    from .delivery import gateway
    p = gateway.download(token, request.client.host if request.client else "")
    return FileResponse(p, media_type="application/zip", filename=p.name)


# ---------------------------------------------------------------------------
# P7: acceptance, warranty, payouts and unit economics
# ---------------------------------------------------------------------------
@router.post("/releases/{rid}/accept")
def release_accept(rid: int, body: dict = Body(default={}), user=ADMIN):
    """Record the client's acceptance (all tasks in the release, or the listed ones)."""
    from .delivery import packager
    from .finance import service as finance
    ids = body.get("task_ids") or packager.get_release(rid)["task_ids"]
    return {"accepted": finance.accept(ids, user["id"])}


@router.post("/finance/auto-accept")
def finance_auto_accept(user=ADMIN):
    from .finance import service as finance
    return {"accepted": finance.auto_accept()}


@router.post("/warranty/claims")
def warranty_file(body: WarrantyClaimIn, user=ADMIN):
    from .finance import service as finance
    try:
        return finance.file_claim(body.task_id, body.defect_tag, body.detail)
    except TransitionError as e:
        conflict(e)


@router.get("/warranty/claims")
def warranty_list(status: str | None = None, user=ADMIN):
    from .finance import service as finance
    return finance.claims(status)


@router.post("/warranty/claims/{cid}/decide")
def warranty_decide(cid: int, body: ClaimDecisionIn, user=ADMIN):
    from .finance import service as finance
    try:
        return finance.decide_claim(cid, user["id"], body.valid, body.resolution, body.note)
    except TransitionError as e:
        conflict(e)


@router.post("/warranty/claims/{cid}/replacement")
def warranty_replacement(cid: int, body: dict = Body(...), user=ADMIN):
    from .finance import service as finance
    return finance.link_replacement(cid, int(body["task_id"]))


@router.get("/finance/batches")
def finance_batches(user=ADMIN):
    from .finance import service as finance
    return finance.batches_overview()


@router.get("/finance/batches/{bid}")
def finance_batch(bid: int, user=ADMIN):
    from .finance import service as finance
    return finance.economics(bid)


@router.get("/finance/payouts/pending")
def finance_pending(user=ADMIN):
    from .finance import service as finance
    return finance.pending()


@router.post("/finance/payouts/run")
def finance_run_payouts(body: dict = Body(default={}), user=ADMIN):
    from .finance import service as finance
    return finance.run_payouts(user["id"], body.get("reference", ""))


@router.get("/finance/payouts/{reference}.csv", response_class=PlainTextResponse)
def finance_payout_csv(reference: str, user=ADMIN):
    from .finance import service as finance
    return finance.payout_csv(reference)


@router.get("/workbench/earnings")
def wb_earnings(user=EXPERT):
    from .finance import service as finance
    return finance.earnings(user["id"])
