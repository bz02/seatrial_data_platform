"""Delivery gateway: signed, expiring download links, a download log, delivery and revocation.

A link is `<release id>.<expiry unix time>.<HMAC-SHA256>` keyed by SEATRAIL_SECRET_KEY, so the client
needs no account on our side and a leaked link dies on its own. Delivering a release moves its
tasks to DELIVERED and starts the warranty clock (P7). Revoking a release that was never delivered
returns its tasks to QA_APPROVED so they can be repackaged; revoking a delivered one only kills
the links.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path

from ... import config, db
from .. import store
from ..models import TaskStatus
from ..state_machine import TransitionError
from .packager import get_release

DAY = 86400


def _mac(msg: str) -> str:
    return hmac.new(config.SECRET_KEY.encode(), f"release:{msg}".encode(), hashlib.sha256).hexdigest()[:32]


def sign(rid: int, expires_at: float) -> str:
    exp = int(expires_at)
    return f"{rid}.{exp}.{_mac(f'{rid}.{exp}')}"


def verify(token: str, now: float | None = None) -> tuple[int, int]:
    try:
        rid_s, exp_s, mac = token.split(".")
        rid, exp = int(rid_s), int(exp_s)
    except ValueError:
        raise PermissionError("malformed download link") from None
    if not hmac.compare_digest(mac, _mac(f"{rid}.{exp}")):
        raise PermissionError("invalid download link")
    if (now or time.time()) > exp:
        raise PermissionError("this download link has expired; ask for a new one")
    return rid, exp


def link(rid: int, ttl_days: float = 7) -> dict:
    r = get_release(rid)
    if r["status"] == "REVOKED":
        raise TransitionError("release is revoked", "release_revoked")
    exp = time.time() + ttl_days * DAY
    db.x("UPDATE releases SET link_expires_at=? WHERE id=?", exp, rid)
    token = sign(rid, exp)
    return {"release_id": rid, "token": token, "path": f"/api/v1/deliveries/{token}", "expires_at": int(exp)}


def deliver(rid: int, actor_id: int | None = None, ttl_days: float = 7) -> dict:
    r = get_release(rid)
    if r["status"] != "BUILT":
        raise TransitionError(f"release is {r['status']}", "not_deliverable")
    for tid in r["task_ids"]:
        store.transition(tid, TaskStatus.DELIVERED, actor_id, f"delivered in release {rid}")
    db.x("UPDATE releases SET status='DELIVERED', delivered_at=? WHERE id=?", db.now(), rid)
    return {"release": get_release(rid), "link": link(rid, ttl_days)}


def revoke(rid: int, actor_id: int | None = None, reason: str = "") -> dict:
    r = get_release(rid)
    if r["status"] == "REVOKED":
        return r
    if r["status"] == "BUILT":
        for tid in r["task_ids"]:
            store.transition(tid, TaskStatus.QA_APPROVED, actor_id, f"release {rid} revoked before delivery: {reason}")
    db.x("UPDATE releases SET status='REVOKED', link_expires_at=? WHERE id=?", db.now(), rid)
    return get_release(rid)


def download(token: str, client_ip: str = "") -> Path:
    rid = None
    try:
        rid, exp = verify(token)
        r = get_release(rid)
        if r["status"] != "DELIVERED":
            raise PermissionError(f"release is {r['status'].lower()}")
        if int(r["link_expires_at"] or 0) != exp:
            raise PermissionError("this download link was replaced by a newer one")
        p = Path(r["zip_path"])
        if not p.exists():
            raise KeyError("release archive is missing on the server")
    except (PermissionError, KeyError) as e:
        if rid is not None and db.q1("SELECT 1 FROM releases WHERE id=?", rid):
            db.x("INSERT INTO download_events (release_id, ok, detail, client_ip, created_at) VALUES (?,?,?,?,?)",
                 rid, 0, str(e), client_ip, db.now())
        raise
    db.x("INSERT INTO download_events (release_id, ok, detail, client_ip, created_at) VALUES (?,?,?,?,?)",
         rid, 1, "", client_ip, db.now())
    return p


def downloads(rid: int) -> list[dict]:
    return db.q("SELECT ok, detail, client_ip, created_at FROM download_events WHERE release_id=? ORDER BY id", rid)
