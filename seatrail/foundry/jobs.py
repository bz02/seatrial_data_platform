"""Background jobs (calibration runs) with their status in the `jobs` table.

An in-process thread pool keeps the deployment to one service. The interface (submit -> id, get)
is what a Celery/Dramatiq worker would expose, so moving to Redis-backed workers later only
changes this file.
"""
from __future__ import annotations

import os
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .. import db

_pool = ThreadPoolExecutor(max_workers=int(os.environ.get("SEATRAIL_JOB_WORKERS", "2")),
                           thread_name_prefix="seatrail-job")


def submit(kind: str, params: dict, fn: Callable[[], dict], sync: bool = False) -> dict:
    jid = db.x("INSERT INTO jobs (kind, status, params, created_at) VALUES (?,?,?,?)", kind, "queued",
               db.dumps(params), db.now())

    def run():
        db.x("UPDATE jobs SET status='running' WHERE id=?", jid)
        try:
            result = fn()
            db.x("UPDATE jobs SET status='done', result=?, finished_at=? WHERE id=?", db.dumps(result), db.now(), jid)
        except Exception as e:
            db.x("UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
                 f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}", db.now(), jid)

    if sync:
        run()
    else:
        _pool.submit(run)
    return get(jid)


def get(jid: int) -> dict:
    j = db.q1("SELECT * FROM jobs WHERE id=?", jid)
    if not j:
        raise KeyError(f"job {jid} not found")
    j["params"] = db.loads(j["params"])
    j["result"] = db.loads(j["result"])
    return j
