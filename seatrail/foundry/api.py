"""Foundry HTTP API, versioned under /api/v1. Platform admins only for now; SME roles come with
the dispatch module (P4)."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from .. import config
from . import jobs, store
from .calibrator import calibrate, loss_analyzer
from .calibrator.adapters import default_panel, get_adapter
from .models import (AssignmentIn, CalibrationRunIn, ContractBatchIn, LeakageStatus, PiiScanStatus,
                     RubricCriterionIn, SourceAssetIn, TaskIn, TransitionIn)
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
        return store.update_task_spec(tid, body.get("env"), body.get("sealed"))
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
    return store.add_criterion(tid, body)


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
