"""IP and chain-of-title gate. A SourceAsset stays PENDING (unusable as a seed, unshippable) until:

    license_type set, ai_training_allowed == True, chain_of_title_doc_uri set,
    PII scan CLEAN or REDACTED, and zero OPEN alerts (leakage, PII, secrets).

A borderline file can be let through only with a ComplianceWaiver: an admin, a written reason and
a timestamp, kept forever. A waived quarantined file is copied back into clean/.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from ... import db
from ..state_machine import TransitionError
from . import scrubber
from .service import alerts, refresh_statuses


def blockers(aid: int) -> list[str]:
    a = db.q1("SELECT * FROM source_assets WHERE id=?", aid)
    if not a:
        raise KeyError(f"source asset {aid} not found")
    out = []
    if not a["license_type"]:
        out.append("no license_type")
    if not a["ai_training_allowed"]:
        out.append("license does not allow AI training")
    if not a["chain_of_title_doc_uri"]:
        out.append("no chain-of-title document")
    if a["pii_scan_status"] not in ("CLEAN", "REDACTED"):
        out.append(f"PII scan is {a['pii_scan_status']}")
    if a["outcome_leakage_status"] != "CLEAN":
        out.append(f"outcome leakage is {a['outcome_leakage_status']}")
    open_ = [r for r in alerts(aid) if r["status"] == "OPEN"]
    if open_:
        out.append(f"{len(open_)} unresolved alert(s): " + ", ".join(f"{r['kind']} {r['path']}" for r in open_[:5]))
    return out


def approve(aid: int, admin_id: int | None = None) -> dict:
    b = blockers(aid)
    if b:
        raise TransitionError("compliance gate: " + "; ".join(b), "compliance_blocked")
    db.x("UPDATE source_assets SET gate_status='APPROVED' WHERE id=?", aid)
    db.audit(0, admin_id, "foundry.source.approve", f"source_asset:{aid}")
    from .. import store
    return store.get_source_asset(aid)


def resolve(alert_id: int, admin_id: int, note: str) -> dict:
    """Confirm a quarantine was right (the file stays out of clean/)."""
    r = db.q1("SELECT * FROM ingestion_alerts WHERE id=?", alert_id)
    if not r:
        raise KeyError(f"alert {alert_id} not found")
    if r["status"] != "OPEN":
        raise ValueError("alert is not open")
    db.x("UPDATE ingestion_alerts SET status='RESOLVED', resolved_at=?, detail=json_set(detail,'$.resolution',?)"
         " WHERE id=?", db.now(), f"admin {admin_id}: {note}", alert_id)
    refresh_statuses(r["asset_id"])
    return db.q1("SELECT * FROM ingestion_alerts WHERE id=?", alert_id)


def waive(alert_id: int, admin_id: int, reason: str) -> dict:
    """Let a quarantined borderline file into clean/, with a permanent audit record."""
    if len((reason or "").strip()) < 10:
        raise ValueError("a waiver needs a written reason (at least 10 characters)")
    r = db.q1("SELECT * FROM ingestion_alerts WHERE id=?", alert_id)
    if not r:
        raise KeyError(f"alert {alert_id} not found")
    if r["kind"] in ("SECRET", "FUTURE_COMMIT"):
        raise ValueError(f"{r['kind']} findings cannot be waived")
    a = db.q1("SELECT storage_uri FROM source_assets WHERE id=?", r["asset_id"])
    root = Path(a["storage_uri"]) if a and a["storage_uri"] else None
    if root and (root / "quarantine" / r["path"]).is_file():
        dest = root / "clean" / scrubber.clean_name(r["path"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / "quarantine" / r["path"], dest)
    wid = db.x("INSERT INTO compliance_waivers (asset_id, alert_id, admin_id, reason, created_at) VALUES (?,?,?,?,?)",
               r["asset_id"], alert_id, admin_id, reason.strip(), db.now())
    db.x("UPDATE ingestion_alerts SET status='WAIVED', resolved_at=? WHERE id=?", db.now(), alert_id)
    refresh_statuses(r["asset_id"])
    db.audit(0, admin_id, "foundry.source.waiver", f"alert:{alert_id}", reason=reason.strip())
    return db.q1("SELECT * FROM compliance_waivers WHERE id=?", wid)
