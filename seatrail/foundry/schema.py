"""Foundry tables. Appended to db.SCHEMA so one `executescript` creates everything.

`foundry_tasks` is the unit a buyer accepts or returns. A foundry task of profile
`profile-sheet-accounting` points at a row of the existing `tasks` table, so the accounting
sandbox becomes one environment profile among several instead of a separate system.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS contract_batches (
  id INTEGER PRIMARY KEY,
  client_name TEXT NOT NULL,
  deal_type TEXT NOT NULL CHECK (deal_type IN ('OTS','COMMISSIONED','EVAL')),
  domain TEXT NOT NULL,
  target_pass_rate_min REAL NOT NULL DEFAULT 0.15,
  target_pass_rate_max REAL NOT NULL DEFAULT 0.70,
  warranty_days INTEGER NOT NULL DEFAULT 60,
  price_per_accepted_task_usd REAL NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 0,          -- client-urgent batches dispatch first
  quoted_cost_per_task_usd REAL NOT NULL DEFAULT 0,
  payment_terms_days INTEGER NOT NULL DEFAULT 30,
  created_at REAL NOT NULL,
  CHECK (target_pass_rate_min <= target_pass_rate_max)
);

CREATE TABLE IF NOT EXISTS source_assets (
  id INTEGER PRIMARY KEY,
  source_type TEXT NOT NULL CHECK (source_type IN ('REPO','LEGAL_MATTER','CAD_ASSEMBLY','EHR','FINANCE')),
  raw_uri TEXT NOT NULL,
  content_hash TEXT NOT NULL DEFAULT '',
  license_type TEXT NOT NULL DEFAULT '',
  ai_training_allowed INTEGER NOT NULL DEFAULT 0,
  chain_of_title_doc_uri TEXT NOT NULL DEFAULT '',
  pii_scan_status TEXT NOT NULL DEFAULT 'PENDING'
    CHECK (pii_scan_status IN ('PENDING','CLEAN','REDACTED','BLOCKED')),
  outcome_leakage_status TEXT NOT NULL DEFAULT 'PENDING'
    CHECK (outcome_leakage_status IN ('PENDING','CLEAN','FLAGGED')),
  gate_status TEXT NOT NULL DEFAULT 'PENDING' CHECK (gate_status IN ('PENDING','APPROVED')),
  domain_tag TEXT NOT NULL DEFAULT '',
  acquisition_cost_usd REAL NOT NULL DEFAULT 0,
  storage_uri TEXT NOT NULL DEFAULT '',   -- sources/{sha256}/ (raw/, clean/, manifest.json)
  created_at REAL NOT NULL
);

-- Findings from ingestion: crawler junk, outcome leakage, PII, secrets.
CREATE TABLE IF NOT EXISTS ingestion_alerts (
  id INTEGER PRIMARY KEY,
  asset_id INTEGER NOT NULL REFERENCES source_assets(id),
  kind TEXT NOT NULL CHECK (kind IN ('CRAWLER_ARTIFACT','LEAKAGE','PII','SECRET','UNSCANNABLE','FUTURE_COMMIT')),
  path TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL CHECK (status IN ('OPEN','QUARANTINED','REDACTED','STRIPPED','RESOLVED','WAIVED')),
  created_at REAL NOT NULL,
  resolved_at REAL
);

-- An admin's documented decision to let a borderline file through.
CREATE TABLE IF NOT EXISTS compliance_waivers (
  id INTEGER PRIMARY KEY,
  asset_id INTEGER NOT NULL REFERENCES source_assets(id),
  alert_id INTEGER REFERENCES ingestion_alerts(id),
  admin_id INTEGER NOT NULL REFERENCES users(id),
  reason TEXT NOT NULL CHECK (length(trim(reason)) >= 10),
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS foundry_tasks (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES contract_batches(id),
  domain TEXT NOT NULL,
  archetype TEXT NOT NULL CHECK (archetype IN
    ('MASK_RECONSTRUCT','MULTI_HOP_FLAW','FEATURE_TREE_REBUILD','BUG_FIX','E2E_ENV')),
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','SYNTH_CHECK','CALIBRATING',
    'SME_REVIEW','REWORK','QA_APPROVED','PACKAGED','DELIVERED','ACCEPTED','RETURNED')),
  author_id INTEGER REFERENCES users(id),
  env TEXT NOT NULL,              -- EnvSpec JSON (agent-visible)
  sealed TEXT NOT NULL,           -- SealedSpec, encrypted (grader_isolation.seal)
  verdict TEXT CHECK (verdict IN ('TOO_HARD_0PCT','SWEET_SPOT','SATURATED_100PCT')),
  rework_reason TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS task_source_assets (
  task_id INTEGER NOT NULL REFERENCES foundry_tasks(id),
  asset_id INTEGER NOT NULL REFERENCES source_assets(id),
  PRIMARY KEY (task_id, asset_id)
);

CREATE TABLE IF NOT EXISTS task_transitions (
  id INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES foundry_tasks(id),
  from_status TEXT NOT NULL,
  to_status TEXT NOT NULL,
  actor_id INTEGER,
  reason TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rubric_criteria (
  id INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES foundry_tasks(id),
  kind TEXT NOT NULL CHECK (kind IN ('PROGRAMMATIC','ATOMIC_JUDGE','DECOY_NEGATIVE','EXCLUDED_BOUNDARY')),
  weight REAL NOT NULL DEFAULT 1,
  spec_json TEXT NOT NULL DEFAULT '{}',
  ground_truth_evidence_ref TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);

-- golden=1.0 / empty=0.0 proof. A task cannot leave DRAFT without a passing row for its version.
CREATE TABLE IF NOT EXISTS sanity_checks (
  id INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES foundry_tasks(id),
  task_version INTEGER NOT NULL,
  golden_score REAL NOT NULL,
  empty_score REAL NOT NULL,
  passed INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS calibration_runs (
  id INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES foundry_tasks(id),
  task_version INTEGER NOT NULL,
  model_name TEXT NOT NULL,
  k_rollouts INTEGER NOT NULL,
  pass_count INTEGER NOT NULL,
  pass_at_1 REAL NOT NULL,
  pass_at_k REAL NOT NULL,
  pass_hat_k REAL NOT NULL,         -- pass^k: all k succeed (consistency)
  mean_score REAL NOT NULL,
  submission_rate REAL NOT NULL,
  avg_steps REAL NOT NULL,
  token_cost_usd REAL NOT NULL,
  verdict TEXT NOT NULL CHECK (verdict IN ('TOO_HARD_0PCT','SWEET_SPOT','SATURATED_100PCT')),
  trace_uri TEXT NOT NULL,
  group_id TEXT NOT NULL,           -- one calibration call across the model panel
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rollouts (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES calibration_runs(id),
  k_index INTEGER NOT NULL,
  score REAL NOT NULL,
  passed INTEGER NOT NULL,
  submitted INTEGER NOT NULL,
  steps INTEGER NOT NULL,
  stop_reason TEXT NOT NULL,
  failure_bucket TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  latency_s REAL NOT NULL DEFAULT 0,
  grader TEXT NOT NULL DEFAULT '{}',
  trace_uri TEXT NOT NULL
);

-- Separation of duties: an author never reviews their own task.
CREATE TABLE IF NOT EXISTS sme_assignments (
  id INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES foundry_tasks(id),
  author_id INTEGER NOT NULL REFERENCES users(id),
  reviewer_id INTEGER NOT NULL REFERENCES users(id),
  pod TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  CHECK (author_id != reviewer_id)
);

CREATE TABLE IF NOT EXISTS cost_ledger (
  id INTEGER PRIMARY KEY,
  task_id INTEGER REFERENCES foundry_tasks(id),
  batch_id INTEGER REFERENCES contract_batches(id),
  kind TEXT NOT NULL CHECK (kind IN ('SME_LABOR','COMPUTE','MODEL_API','REWORK')),
  amount_usd REAL NOT NULL,
  ref TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued','running','done','failed')),
  params TEXT NOT NULL DEFAULT '{}',
  result TEXT,
  error TEXT,
  created_at REAL NOT NULL,
  finished_at REAL
);

-- P4: who can author / review what, at what rate and capacity.
CREATE TABLE IF NOT EXISTS expert_profiles (
  user_id INTEGER PRIMARY KEY REFERENCES users(id),
  domain_tags TEXT NOT NULL DEFAULT '[]',     -- e.g. ["MECHE_CAD","CORPORATE_MNA","K8S_SRE"]
  hourly_rate_usd REAL NOT NULL DEFAULT 0,
  per_task_rate_usd REAL NOT NULL DEFAULT 0,
  capacity_cap INTEGER NOT NULL DEFAULT 3,
  pod TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS seed_claims (
  asset_id INTEGER PRIMARY KEY REFERENCES source_assets(id),
  user_id INTEGER NOT NULL REFERENCES users(id),
  task_id INTEGER REFERENCES foundry_tasks(id),
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS qa_reviews (
  id INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES foundry_tasks(id),
  task_version INTEGER NOT NULL,
  reviewer_id INTEGER NOT NULL REFERENCES users(id),
  author_id INTEGER REFERENCES users(id),
  decision TEXT NOT NULL CHECK (decision IN ('APPROVE','RETURN')),
  checklist TEXT NOT NULL DEFAULT '{}',
  defect_tags TEXT NOT NULL DEFAULT '[]',
  notes TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);

-- Every edit, transition, calibration and QA decision. Append-only: triggers refuse changes.
CREATE TABLE IF NOT EXISTS task_audit_events (
  id INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL,
  actor_id INTEGER,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS task_audit_no_update BEFORE UPDATE ON task_audit_events
BEGIN SELECT RAISE(ABORT, 'task audit events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS task_audit_no_delete BEFORE DELETE ON task_audit_events
BEGIN SELECT RAISE(ABORT, 'task audit events are immutable'); END;
"""
