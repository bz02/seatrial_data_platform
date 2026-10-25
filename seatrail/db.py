"""SQLite storage. One file, no ORM, so the MVP stays easy to read and move to Postgres later."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import config

SCHEMA = """
-- Organizations. org 0 is the platform itself (marketplace edition).
CREATE TABLE IF NOT EXISTS orgs (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('platform','enterprise')),
  policy TEXT NOT NULL DEFAULT '{}',   -- JSON capture/privacy policy (enterprise)
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  org_id INTEGER NOT NULL REFERENCES orgs(id),
  -- marketplace: expert, buyer, admin, seller.  enterprise: employee, steward, ml_engineer, org_admin
  role TEXT NOT NULL CHECK (role IN ('expert','buyer','admin','seller',
                                     'employee','steward','ml_engineer','org_admin')),
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  credentials TEXT DEFAULT '',
  consent_at REAL,                     -- enterprise employees must opt in before capture
  capture_token TEXT UNIQUE,           -- used by the browser recorder
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  template TEXT NOT NULL,
  domain TEXT NOT NULL,
  difficulty TEXT NOT NULL,
  seed INTEGER NOT NULL,
  title TEXT NOT NULL,
  brief TEXT NOT NULL,
  initial_state TEXT NOT NULL,   -- JSON shown to the expert
  answer_key TEXT NOT NULL,      -- JSON, never sent to the client
  reward_cents INTEGER NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  UNIQUE (template, seed)
);

-- A session is one episode of work. source='sandbox' (marketplace task) or 'capture' (real work).
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY,
  org_id INTEGER NOT NULL REFERENCES orgs(id),
  source TEXT NOT NULL CHECK (source IN ('sandbox','capture')),
  task_id INTEGER REFERENCES tasks(id),
  user_id INTEGER NOT NULL REFERENCES users(id),
  status TEXT NOT NULL CHECK (status IN ('in_progress','checked','submitted','discarded')),
  goal TEXT DEFAULT '',          -- capture: what the employee was trying to do
  outcome TEXT DEFAULT '',       -- capture: success | partial | failed
  state TEXT NOT NULL,           -- sandbox state JSON, or capture metadata JSON
  started_at REAL NOT NULL,
  ended_at REAL,
  last_report TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  seq INTEGER NOT NULL,
  ts REAL NOT NULL,              -- seconds since session start
  type TEXT NOT NULL,
  payload TEXT NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0,  -- employee removed this step before submitting
  UNIQUE (session_id, seq)
);

CREATE TABLE IF NOT EXISTS records (
  id INTEGER PRIMARY KEY,
  org_id INTEGER NOT NULL REFERENCES orgs(id),
  source TEXT NOT NULL,
  session_id INTEGER NOT NULL UNIQUE REFERENCES sessions(id),
  task_id INTEGER REFERENCES tasks(id),
  user_id INTEGER NOT NULL REFERENCES users(id),
  template TEXT NOT NULL,        -- task template, or workflow label for captures
  record TEXT NOT NULL,          -- training record JSON (seatrail.trajectory.v1)
  verification TEXT NOT NULL,
  judge TEXT NOT NULL,
  pii TEXT NOT NULL,
  fingerprint TEXT NOT NULL,     -- MinHash signature of the narration
  flags TEXT NOT NULL,
  quality_score REAL NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('needs_review','approved','rejected')),
  reviewer_id INTEGER REFERENCES users(id),
  review_notes TEXT DEFAULT '',
  created_at REAL NOT NULL,
  reviewed_at REAL
);

-- Enterprise: export jobs that turn approved records into post-training files.
CREATE TABLE IF NOT EXISTS exports (
  id INTEGER PRIMARY KEY,
  org_id INTEGER NOT NULL REFERENCES orgs(id),
  requested_by INTEGER NOT NULL REFERENCES users(id),
  format TEXT NOT NULL,
  filters TEXT NOT NULL,
  record_ids TEXT NOT NULL,
  path TEXT NOT NULL,
  created_at REAL NOT NULL
);

-- Enterprise: every sensitive read and write is logged.
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  org_id INTEGER NOT NULL,
  user_id INTEGER,
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);

-- Marketplace
CREATE TABLE IF NOT EXISTS datasets (
  id INTEGER PRIMARY KEY,
  seller_id INTEGER NOT NULL,    -- 0 = platform (phase 1); a seller user id in the open market
  name TEXT NOT NULL,
  domain TEXT NOT NULL,
  description TEXT NOT NULL,
  price_cents INTEGER NOT NULL,
  license TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('draft','listed','unlisted')),
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_items (
  dataset_id INTEGER NOT NULL REFERENCES datasets(id),
  record_id INTEGER NOT NULL REFERENCES records(id),
  PRIMARY KEY (dataset_id, record_id)
);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY,
  dataset_id INTEGER NOT NULL REFERENCES datasets(id),
  buyer_id INTEGER NOT NULL REFERENCES users(id),
  seller_id INTEGER NOT NULL,
  gross_cents INTEGER NOT NULL,
  take_rate REAL NOT NULL,
  platform_fee_cents INTEGER NOT NULL,
  seller_net_cents INTEGER NOT NULL,
  expert_pool_cents INTEGER NOT NULL,
  payment_ref TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE (dataset_id, buyer_id)
);

CREATE TABLE IF NOT EXISTS payouts (
  id INTEGER PRIMARY KEY,
  expert_id INTEGER NOT NULL REFERENCES users(id),
  kind TEXT NOT NULL CHECK (kind IN ('task_reward','revenue_share')),
  amount_cents INTEGER NOT NULL,
  record_id INTEGER REFERENCES records(id),
  order_id INTEGER REFERENCES orders(id),
  created_at REAL NOT NULL
);
"""

def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_path: Path | None = None


def conn() -> sqlite3.Connection:
    global _conn, _conn_path
    with _lock:
        if _conn is None or _conn_path != config.DB_PATH:
            _conn = _connect(config.DB_PATH)
            _conn_path = config.DB_PATH
            _conn.executescript(SCHEMA)
        return _conn


def reset(path: Path | None = None) -> None:
    """Point at a (new) database file. Used by tests and the seed script."""
    global _conn, _conn_path
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        if path is not None:
            config.DB_PATH = path


@contextmanager
def tx():
    c = conn()
    with _lock:
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise


def q(sql: str, *args) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql: str, *args) -> dict | None:
    rows = q(sql, *args)
    return rows[0] if rows else None


def x(sql: str, *args) -> int:
    with _lock:
        cur = conn().execute(sql, args)
        return cur.lastrowid


def now() -> float:
    return time.time()


def dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def loads(s):
    return json.loads(s) if s else None


def audit(org_id: int, user_id: int | None, action: str, target: str, **detail) -> None:
    x("INSERT INTO audit_log (org_id, user_id, action, target, detail, created_at) VALUES (?,?,?,?,?,?)",
      org_id, user_id, action, target, dumps(detail), now())
