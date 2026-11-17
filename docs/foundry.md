# Foundry: the verifiable-task production line

Foundry is the part of SeaTrial that produces sellable RL environments and eval tasks: a task is
written, proven solvable and fairly graded in a pinned sandbox, calibrated against frontier models
until it sits in the difficulty band a buyer pays for, reviewed by a second expert, packaged and
delivered. It lives in `seatrail/foundry/` and shares the database, auth and the accounting sandbox
with the rest of the app.

Built so far (phase 1): **P0** core model and state machine, **P2** sandbox and anti-cheat
grading, **P3** difficulty calibration and Loss Analysis reports.

## Module map

The original plan described a separate `foundry-data-os` monorepo with `services/*`. We kept one
repo and one package instead; the mapping is:

| Plan | Here |
|---|---|
| P0 core models, state machine, API contract | `foundry/models.py`, `schema.py`, `state_machine.py`, `store.py`, `api.py` |
| P2 `services/sandbox_runner` | `foundry/sandbox_runner/` (`images/`, `tools.py`, `tool_server.py`, `backends.py`, `env.py`, `grader_isolation.py`, `verifiers/`, `runner.py`) |
| P3 `services/calibrator` | `foundry/calibrator/` (`adapters.py`, `calibrate.py`, `loss_analyzer.py`) |
| async job queue | `foundry/jobs.py` |

## Task lifecycle

```
DRAFT -> SYNTH_CHECK -> CALIBRATING -> SME_REVIEW -> QA_APPROVED -> PACKAGED -> DELIVERED -> ACCEPTED
                             |              |              |                          |
                             v              v              v                          v
                           REWORK <---------+--------------+------------------- RETURNED
                             |
                             +--> SYNTH_CHECK
```

Server-side guards (illegal or unguarded moves return **409** with a `code`):

| Move | Guard | code |
|---|---|---|
| DRAFT/REWORK -> SYNTH_CHECK | passing sanity check (golden = 1.0, empty = 0.0) for the current task version | `sanity_check_required` |
| -> CALIBRATING | at least one rubric criterion; no source FLAGGED for leakage or BLOCKED by PII | `rubric_required`, `source_blocked` |
| CALIBRATING -> SME_REVIEW | latest calibration verdict is SWEET_SPOT for this version | `calibration_required` |
| -> QA_APPROVED | an active reviewer who is not the author; the approver is not the author | `reviewer_required`, `conflict_of_interest` |
| QA_APPROVED and later | every source clean, licensed for AI training, with license and chain-of-title | `source_blocked` |

Editing a task's environment or ground truth is only allowed in DRAFT or REWORK and bumps its
version, so an old sanity check or calibration no longer counts. `sme_assignments` also has a
database CHECK that author and reviewer differ.

## Sandbox and anti-cheat grading (P2)

- **Pinned images** in `sandbox_runner/images/`: `profile-code-swe`, `profile-doc-office`,
  `profile-cad-meche` (build with `images/build.sh`). The existing spreadsheet/inbox sandbox is a
  fourth profile, `profile-sheet-accounting`, and every existing accounting task can be imported as
  a foundry task (`POST /api/v1/tasks/import-sheet`). All 12 seeded tasks pass the sanity check.
- **Tools** in two modes: `auto` (bash, code_exec, view_image, finish) and `agentic_native` (bash,
  read, write, edit, glob, grep, view_image, finish). `tool_server.py` serves them inside the image
  as an MCP-compatible JSON-RPC server (stdio or HTTP).
- **Ground truth never reaches the agent.** The sealed bundle (hidden tests, answer key, golden
  solution) is encrypted at rest (Fernet; key `SEATRAIL_GRADER_KEY`, derived from
  `SEATRAIL_SECRET_KEY` if unset). Grading happens host-side after the sandbox stops, in a fresh
  0700 directory that holds only the deliverables, and is deleted afterwards. Agent processes get a
  scrubbed environment. A test runs a malicious agent (`find / -name "*truth*"`, `env`, grep for a
  marker inside the hidden tests) and asserts it finds nothing.
- **Graders** (`verifiers/`), all returning `GraderResult {overall_score, passed, sub_metrics,
  criteria_results, failure_mode}`: pytest with fail_to_pass / pass_to_pass (regressions score 0),
  docx tracked changes + structure, CAD volume/bbox/Chamfer with an all-or-nothing 0.95 gate,
  text/JSON file match, and the accounting answer key. None gives credit for doing nothing.
- **Limits:** per-command timeout, step limit, wall clock, CPU/RAM/PID caps, network off unless
  allowlisted, at most `SEATRAIL_MAX_SANDBOXES` concurrent sandboxes, and a circuit breaker that
  stops a calibration after 3 consecutive environment failures instead of recording fake 0% results.

`SEATRAIL_SANDBOX_BACKEND=docker` runs each rollout in its own container (`--network none`,
`--read-only`, `--cap-drop ALL`, non-root). The default `local` backend runs the same tools as
rlimited subprocesses; it is what the tests use and is fine for the accounting profile, but it does
not isolate the filesystem or network, so **code tasks from untrusted models should run on a host
with Docker** (Render web services cannot run Docker; use a separate sandbox worker there).

## Calibration and Loss Analysis (P3)

- **Adapters** (`calibrator/adapters.py`): `claude-*` through the Anthropic SDK (adaptive thinking,
  server-side refusal fallback), `openai:<model>` for any OpenAI-compatible endpoint such as a
  LiteLLM proxy or Gemini's compatible API (`SEATRAIL_OPENAI_BASE_URL`, `SEATRAIL_OPENAI_API_KEY`),
  and `MockAdapter` scripted models. Tests use only mocks; no key is needed.
- **Calibration** (`calibrate.py`): K rollouts per model (default 4) in parallel, each graded in
  isolation. Records pass@1, unbiased pass@k, pass^k, mean score, submission rate, steps and USD
  cost per model in `calibration_runs`, every rollout in `rollouts`, full JSONL traces under
  `DATA_DIR/traces/`, and model spend in `cost_ledger`. Pooled panel pass rate decides the route:
  0 passes or below the batch's `target_pass_rate_min` -> TOO_HARD -> REWORK with the top failure
  modes; above `target_pass_rate_max` -> SATURATED -> REWORK ("harder-evolution"); otherwise
  SWEET_SPOT -> SME_REVIEW.
- **Failure taxonomy** (`loss_analyzer.py`): TOOL_LOOP_STALL, MISSING_DELIVERABLE_FILE,
  PARTIAL_DOC_READ_OMISSION, SCHEMA_OR_FORMAT_VIOLATION, VERIFY_PRECISION_MISS,
  HALLUCINATED_CITATION, ENVIRONMENT_ERROR.
- **Report** per batch as JSON, Markdown or printable HTML (browser "Save as PDF"): pass@1/pass@k/
  pass^k by model (optionally anonymized as Model A/B), failure distribution with a chart, 2 to 3
  anonymized and PII-redacted failure excerpts, and the golden 100% / empty 0% sanity proof.

## API (`/api/v1`, platform admin)

| Method | Path | |
|---|---|---|
| POST | `/batches` | create a ContractBatch |
| GET | `/batches/{id}` | batch with task counts by status |
| POST | `/source-assets`, `/source-assets/{id}/scan` | register a source; set PII / leakage status |
| POST | `/tasks`, `/tasks/import-sheet` | create a task; wrap an accounting task |
| GET / PATCH | `/tasks/{id}` | task (never the sealed bundle) + history + cost; edit in DRAFT/REWORK |
| POST | `/tasks/{id}/criteria`, `/tasks/{id}/assignments` | rubric criterion; author/reviewer pair |
| POST | `/tasks/{id}/sanity-check` | golden = 1.0 / empty = 0.0 proof |
| POST | `/tasks/{id}/transition` | `{to, reason}`; 409 on illegal moves |
| POST | `/calibration/run` | `{task_id or batch_id, models, k, concurrency}` -> job (`?sync=true` to wait) |
| GET | `/jobs/{id}` | job status and result |
| GET | `/calibration/batch/{id}/report` | `?format=json|md|html&anonymize=true` |

## Stack decisions

The plan called for Python 3.12 + FastAPI + Pydantic v2 + SQLAlchemy/Alembic + Postgres + Redis/
Celery + Next.js + MinIO. We kept what the product already runs and added only what phase 1 needs:

| Plan | Now | Why / when to switch |
|---|---|---|
| FastAPI, Pydantic v2 | same | already the app's stack |
| SQLAlchemy 2 + Alembic + Postgres 16 | plain SQL on SQLite, `CREATE TABLE IF NOT EXISTS` | one small service on one disk is enough for the first contracts; the SQL is portable. Move to Postgres + Alembic when there is more than one writer or a second deployment |
| Redis + Celery/Dramatiq | in-process thread pool with a `jobs` table (`foundry/jobs.py`) | same submit/get interface, no extra service to run on Render; swap when calibration volume needs separate workers |
| Next.js + shadcn/ui workbench | not yet; existing pages are plain HTML/JS | the SME workbench is P4; we will decide then whether to add a Next.js app beside the API |
| S3 / MinIO | traces as JSONL files under `DATA_DIR/traces` | one function to change for object storage when traces outgrow the disk |
| uv/poetry, mypy --strict | pip + requirements.txt, ruff-clean new code | keeps the existing Dockerfile and deploy unchanged |
| Separate `foundry-data-os` repo | `seatrail/foundry/` package | one deploy, shared auth and users, and the existing accounting tasks become foundry tasks for free |

Deployment is unchanged: the app stays at `data.seatrial.ai`, separate from the main seatrial.ai
site.

## Next phases

- **P1** ingestion, PII/secret scrubbing, outcome-leakage scrubber, license / chain-of-title gate
  (sets the `source_assets` statuses the state machine already enforces).
- **P4** SME dispatch by domain tag and capacity, authoring pods, workbench with trajectory replay,
  rubric editor, rework queue.
- **P5** synthesis from seeds with a "find the counter-example" critic.
- **P6** packaging to buyer formats, smoke tests, version hashes, delivery.
- **P7** unit-economics ledger (SME hours, compute, model API, rework), 60-day warranty tracking.
- **P8** drift monitor: re-calibrate when a new frontier model ships and flag saturated tasks.
