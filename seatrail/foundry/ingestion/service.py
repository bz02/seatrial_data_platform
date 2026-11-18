"""Ingestion pipeline: snapshot -> classify -> scrub -> redact -> manifest + alerts + statuses.

    sources/{sha256}/raw/                    immutable original snapshot
    sources/{sha256}/clean/                  redacted copies with clean filenames (task material)
    sources/{sha256}/manifest.json           per file: path, clean path, MIME, size, sha256, role, action
    sources/{sha256}/redaction_report.json   per finding: file, line, kind, count (never the value)
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from ... import db
from ..models import LeakageStatus, PiiScanStatus, SourceAssetIn
from . import connectors, scrubber
from .privacy_gate import Scanner


def _alert(aid: int, kind: str, path: str, status: str, **detail) -> None:
    db.x("INSERT INTO ingestion_alerts (asset_id, kind, path, detail, status, created_at, resolved_at)"
         " VALUES (?,?,?,?,?,?,?)", aid, kind, path, db.dumps(detail), status, db.now(),
         None if status == "OPEN" else db.now())


def ingest(asset: SourceAssetIn, *, folder: str | None = None, git_url: str | None = None,
           base_commit: str | None = None, client_names: list[str] | None = None, domain_tag: str = "",
           acquisition_cost_usd: float = 0.0) -> dict:
    if bool(folder) == bool(git_url):
        raise ValueError("give exactly one of folder or git_url")
    if git_url and not base_commit:
        raise ValueError("git sources need base_commit: the last commit the agent may see")
    snap = connectors.snapshot_git(git_url, base_commit) if git_url else connectors.snapshot_folder(folder)

    aid = db.x("INSERT INTO source_assets (source_type, raw_uri, content_hash, license_type, ai_training_allowed,"
               " chain_of_title_doc_uri, pii_scan_status, outcome_leakage_status, domain_tag, acquisition_cost_usd,"
               " storage_uri, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", asset.source_type,
               asset.raw_uri or (git_url or folder), snap.sha256, asset.license_type, int(asset.ai_training_allowed),
               asset.chain_of_title_doc_uri, PiiScanStatus.PENDING, LeakageStatus.PENDING, domain_tag,
               acquisition_cost_usd, str(snap.root), db.now())
    if snap.stripped_commits:
        _alert(aid, "FUTURE_COMMIT", ".git", "STRIPPED", base_commit=snap.base_commit,
               commits=snap.stripped_commits)

    clean = snap.root / "clean"
    quarantine = snap.root / "quarantine"
    if clean.exists():   # same content ingested again: rebuild the derived copies
        shutil.rmtree(clean)
        shutil.rmtree(quarantine, ignore_errors=True)
    clean.mkdir()
    quarantine.mkdir()
    names = scrubber.clean_names([p for p in snap.files if not p.startswith(".git/")])
    scanner = Scanner(client_names)
    manifest, report = [], []
    git_files = 0
    for rel, digest in sorted(snap.files.items()):
        data = (snap.raw / rel).read_bytes()
        role = scrubber.role_of(rel)
        entry = {"path": rel, "clean_path": names.get(rel), "mime": connectors.mime_of(rel), "size": len(data),
                 "sha256": digest, "role": role}
        if rel.startswith(".git/"):
            git_files += 1
            entry["action"] = "kept_in_raw_only"
        elif role == "CRAWLER_ARTIFACT":
            entry["action"] = "quarantined"
            _alert(aid, "CRAWLER_ARTIFACT", rel, "QUARANTINED")
        elif role == "POTENTIAL_LEAKAGE":
            entry["action"] = "quarantined"
            _alert(aid, "LEAKAGE", rel, "OPEN", reason="filename matches an outcome-leakage pattern")
        else:
            res = scanner.scan(rel, data)
            if not res.scanned:
                entry["action"] = "quarantined"
                _alert(aid, "UNSCANNABLE", rel, "QUARANTINED", mime=entry["mime"])
            else:
                text_hits = []
                if entry["mime"].startswith("text") or rel.endswith((".md", ".txt")):
                    text_hits = scrubber.boilerplate_hits(res.data.decode("utf-8", "replace"))
                entry["action"] = "redacted" if res.findings else "copied"
                entry["boilerplate_lines"] = text_hits
                dest = clean / entry["clean_path"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(res.data)
                for f in res.findings:
                    report.append({"path": f.path, "line": f.line, "kind": f.kind, "count": f.count})
                kinds = sorted({f.kind for f in res.findings})
                if any(k.startswith("SECRET") for k in kinds):
                    _alert(aid, "SECRET", rel, "REDACTED", kinds=[k for k in kinds if k.startswith("SECRET")])
                if any(k.startswith("PII") for k in kinds):
                    _alert(aid, "PII", rel, "REDACTED", kinds=[k for k in kinds if k.startswith("PII")])
        if entry["action"] == "quarantined":
            q = quarantine / rel
            q.parent.mkdir(parents=True, exist_ok=True)
            q.write_bytes(data)
        manifest.append(entry)
    if git_files:
        _alert(aid, "UNSCANNABLE", ".git", "QUARANTINED", files=git_files,
               note="trimmed git history stays in raw/; clean/ holds the working tree at base_commit")
    (snap.root / "manifest.json").write_text(json.dumps({"asset_id": aid, "sha256": snap.sha256,
                                                        "base_commit": snap.base_commit, "files": manifest}, indent=1))
    (snap.root / "redaction_report.json").write_text(json.dumps(report, indent=1))
    refresh_statuses(aid)
    from .. import store
    return {**store.get_source_asset(aid), "manifest": manifest, "redactions": len(report),
            "alerts": alerts(aid)}


def alerts(aid: int) -> list[dict]:
    rows = db.q("SELECT * FROM ingestion_alerts WHERE asset_id=? ORDER BY id", aid)
    for r in rows:
        r["detail"] = db.loads(r["detail"])
    return rows


def refresh_statuses(aid: int) -> None:
    rows = alerts(aid)
    pii = PiiScanStatus.REDACTED if any(r["kind"] in ("PII", "SECRET") for r in rows) else PiiScanStatus.CLEAN
    if any(r["kind"] in ("PII", "SECRET") and r["status"] == "OPEN" for r in rows):
        pii = PiiScanStatus.BLOCKED
    leak = LeakageStatus.FLAGGED if any(r["kind"] == "LEAKAGE" and r["status"] == "OPEN" for r in rows) \
        else LeakageStatus.CLEAN
    db.x("UPDATE source_assets SET pii_scan_status=?, outcome_leakage_status=? WHERE id=?", pii, leak, aid)


def clean_dir(aid: int) -> Path:
    a = db.q1("SELECT storage_uri FROM source_assets WHERE id=?", aid)
    if not a or not a["storage_uri"]:
        raise KeyError(f"source asset {aid} has no ingested files")
    return Path(a["storage_uri"]) / "clean"
