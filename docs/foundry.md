# Foundry: the verifiable-task production line

Foundry is the part of SeaTrial that produces sellable RL environments and eval tasks: a task is
written, proven solvable and fairly graded in a pinned sandbox, calibrated against frontier models
until it sits in the difficulty band a buyer pays for, reviewed by a second expert, packaged and
delivered. It lives in `seatrail/foundry/` and shares the database, auth and the accounting sandbox
with the rest of the app.

All nine modules are built: **P0** core model and state machine, **P1** ingestion and compliance
gate, **P2** sandbox and anti-cheat grading, **P3** difficulty calibration and Loss Analysis,
**P4** SME dispatch, workbench and QA, **P5** hybrid synthesis, **P6** packaging and delivery,
**P7** unit economics, payouts and warranty, **P8** drift monitoring and harder evolutions.
Every model call goes through a mockable adapter; the whole test suite runs without an API key.

## Module map

The original plan described a separate `foundry-data-os` monorepo with `services/*`. We kept one
repo and one package instead; the mapping is:

| Plan | Here |
|---|---|
| P0 core models, state machine, API contract | `foundry/models.py`, `schema.py`, `state_machine.py`, `store.py`, `api.py` |
| P1 ingestion / redaction / compliance | `foundry/ingestion/` (`connectors.py`, `scrubber.py`, `privacy_gate.py`, `compliance_gate.py`, `service.py`) |
| P2 `services/sandbox_runner` | `foundry/sandbox_runner/` (`images/`, `tools.py`, `tool_server.py`, `backends.py`, `env.py`, `grader_isolation.py`, `verifiers/`, `runner.py`) |
| P3 `services/calibrator` | `foundry/calibrator/` (`adapters.py`, `calibrate.py`, `loss_analyzer.py`) |
| P4 dispatch + workbench + QA | `foundry/dispatch/` (`rules.py`, `service.py`), page `/workbench` |
| P5 synthesizer | `foundry/synthesizer/` (`llm.py`, `pipeline.py`) |
| P6 packaging + delivery gateway | `foundry/delivery/` (`packager.py`, `gateway.py`) |
| P7 finance | `foundry/finance/service.py`, page `/finance` |
| P8 drift + evolution | `foundry/drift/` (`monitor.py`, `__main__.py` for cron) |
| async job queue | `foundry/jobs.py` |

## Task lifecycle

```
DRAFT -> SYNTH_CHECK -> CALIBRATING -> SME_REVIEW -> QA_APPROVED -> PACKAGED -> DELIVERED -> ACCEPTED
                             |              |              |    ^          |            |          |
                             v              v              v    +----------+            v          |
                           REWORK <---------+--------------+------------------- RETURNED <---------+
                             |                                (revoked before delivery)   (inside the warranty)
                             +--> SYNTH_CHECK
```

Server-side guards (illegal or unguarded moves return **409** with a `code`):

| Move | Guard | code |
|---|---|---|
| DRAFT/REWORK -> SYNTH_CHECK | passing sanity check (golden = 1.0, empty = 0.0) for the current task version | `sanity_check_required` |
| -> CALIBRATING | at least one rubric criterion; no source FLAGGED for leakage or BLOCKED by PII | `rubric_required`, `source_blocked` |
| CALIBRATING -> SME_REVIEW | latest calibration verdict is SWEET_SPOT for this version | `calibration_required` |
| -> QA_APPROVED | an active reviewer who is not the author; the approver is not the author | `reviewer_required`, `conflict_of_interest` |
| -> SYNTH_CHECK, -> CALIBRATING | prompt linter has no blocking phrase; rubric criteria cite evidence, use no subjective words, review/drafting archetypes have a decoy | `authoring_rules` |
| -> QA_APPROVED | reviewer is not in the author's pod | `conflict_of_interest` |
| QA_APPROVED and later | every source clean, licensed for AI training, with license and chain-of-title, and through the compliance gate | `source_blocked` |
| DELIVERED/ACCEPTED -> RETURNED | inside the batch's warranty window | `warranty_expired` |

Editing a task's environment or ground truth is only allowed in DRAFT or REWORK and bumps its
version, so an old sanity check or calibration no longer counts. `sme_assignments` also has a
database CHECK that author and reviewer differ. Every create, edit, criterion, assignment,
transition, sanity check, calibration, QA decision, warranty event and evolution is written to
`task_audit_events`, which SQLite triggers make append-only.

## Ingestion and compliance gate (P1)

- **Connectors** snapshot a folder or a git repository into content-addressed, read-only storage
  (`SEATRAIL_OBJECT_ROOT`). Git sources are cut at `base_commit`: later commits, other refs, tags
  and the reflog are removed, so the fix a task is built from cannot leak through history.
- **Outcome-leakage scrubber** strips crawler file-name prefixes and boilerplate and quarantines
  files that look like answers (solutions, graded copies, future commits).
- **Privacy gate** redacts PII (emails, phones, names of the client) and secrets (keys, tokens,
  private keys), including inside .docx XML, and writes a redaction report.
- **Compliance gate**: an admin approves a source only when nothing is open. Alerts can be resolved
  or waived with a written reason; secrets and future commits cannot be waived. Only approved
  sources can be claimed as seeds or ship.

## SME dispatch, workbench and QA (P4)

- Expert profiles carry verified domain tags, rates, a capacity cap and an authoring pod. Queues
  show only matching domains, never more than the cap, ordered by client priority, then rework,
  then age. Reviewers never see their own tasks or their pod's; `POST /dispatch/auto` assigns open
  reviews to the eligible reviewer with the best QA record and the lightest load.
- `/workbench` (experts): claim a seed and browse its clean files, write the prompt with a live
  linter, build the rubric with live criterion checks, run the sanity check and submit; replay any
  calibration rollout turn by turn; as a reviewer, see the grader, tick every criterion, approve or
  return with defect tags (LEAKAGE, BAD_VERIFIER, AMBIGUOUS_PROMPT, TOO_EASY, UNSOLVABLE) and notes.

## Hybrid synthesis (P5)

Audit -> Transform -> Rules -> Devil's Advocate -> Sanity, on a compliance-approved seed. An author
model proposes candidates with line ranges and writes the task (prompt, edits that produce the
workspace, hidden tests or expected answers, atomic rubric). Deterministic rules reject edits that do
not apply, prompts that give the construction away or quote the answer, missing tests, and rubric
rule breaks. A *different* critic model sees only what the agent sees plus a summary of the grader
and looks for leakage, ambiguity, unsolvability, easiness and bad verifiers; a BLOCKER goes back to
the author once with the critique. Survivors must still score golden 1.0 / empty 0.0. They become
DRAFT tasks (origin SYNTH) owned by an SME, who stays accountable. Every candidate and the stage
that stopped it is in `synth_runs`; rejected spend is booked against the batch.

## Packaging and delivery (P6)

`POST /batches/{id}/releases` turns QA-approved tasks into `release-N/` + `release-N.zip`:
`manifest.json` (sha256 of every file), `datasheet.md`, `tasks.jsonl` (agent-visible task, rubric,
measured difficulty, provenance), `ground_truth.jsonl` and, for OTS/COMMISSIONED deals only, scored
trajectories. Every record carries a contamination canary. Final gates: compliance, a last PII and
secret scan, and exact plus near-duplicate checks (5-gram Jaccard >= 0.85) against every earlier
release, so a task is never sold twice and an eval set never overlaps a training set (a harder
evolution may resemble its parent only when it goes to the same client). Delivery issues signed,
expiring links (`/api/v1/deliveries/<token>`, HMAC with `SEATRAIL_SECRET_KEY`); a new link replaces
the old one, revocation kills links, and every download attempt is logged.

## Unit economics, payouts and warranty (P7)

- Earnings are booked from transitions: on QA approval the author earns their per-task rate (80%
  payable, 20% held until the client accepts) and the reviewer a review fee (30% of their rate); a
  QA return books the review fee as REWORK. Authors are paid once per task.
- Revenue is recognised on acceptance (explicit, or deemed after `SEATRAIL_ACCEPTANCE_DAYS`, 14).
- Warranty: within `warranty_days` (60 by default) of delivery the client can claim a defect. A
  valid claim sends the task back to REWORK and is resolved by a replacement (booked at the
  original production cost) or a credit. Author-attributable defects forfeit or claw back the
  holdback; TOO_EASY is model drift and stays our risk.
- `/finance` and `GET /finance/batches/{id}`: funnel, revenue, cost by kind (SME labour, rework,
  model API, sandbox compute at `SEATRAIL_SANDBOX_USD_PER_ROLLOUT`, source acquisition spread over
  the tasks built from each source, warranty replacements), synthesis waste, cost per accepted task
  against the quote, gross margin, QA return rate, warranty reserve, payouts owed and receivables
  by release with their due dates. Payout runs pay positive balances and export a CSV.

## Drift monitoring and harder evolutions (P8)

`POST /drift/run` (or `python -m seatrail.foundry.drift` from a weekly cron) re-runs shipped tasks on
the current panel (`SEATRAIL_DRIFT_MODELS`) without changing their status or calibrated difficulty.
Above the batch's band a task is SATURATED; a rise of `SEATRAIL_DRIFT_DELTA` (0.25) is DRIFTING.
Saturated tasks get one harder evolution: the author model gets the parent's prompt, grader and
solved repository, and the child goes through the same rules, critic and sanity check as P5, then
back to the parent's author as a DRAFT (origin EVOLVED). Evolutions are also what TOO_EASY warranty
replacements are made from (`POST /warranty/claims/{id}/replacement`).

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
| POST | `/ingestion/scan`, `/ingestion/{id}/approve`, `/ingestion/alerts/{id}/resolve|waive` | P1 snapshot, scan and gate |
| PUT / POST / GET | `/experts/{uid}/profile`, `/dispatch/auto`, `/tasks/{id}/audit` | P4 admin |
| * | `/workbench/...` | P4 expert API: queue, seeds, tasks, criteria, submit, claim-review, qa, rollouts, lint, earnings |
| POST / GET | `/synth/runs` | P5 synthesis job; candidate log |
| POST / GET | `/batches/{id}/releases`, `/releases/{id}`, `/releases/{id}/verify|deliver|link|revoke|accept` | P6 releases |
| GET | `/deliveries/{token}` | public, signed download |
| * | `/warranty/claims`, `/finance/batches`, `/finance/payouts/pending|run`, `/finance/payouts/{ref}.csv`, `/finance/auto-accept` | P7 |
| POST / GET | `/drift/run`, `/drift/reports`, `/tasks/{id}/evolve` | P8 |

## Stack decisions

The plan called for Python 3.12 + FastAPI + Pydantic v2 + SQLAlchemy/Alembic + Postgres + Redis/
Celery + Next.js + MinIO. We kept what the product already runs and added only what phase 1 needs:

| Plan | Now | Why / when to switch |
|---|---|---|
| FastAPI, Pydantic v2 | same | already the app's stack |
| SQLAlchemy 2 + Alembic + Postgres 16 | plain SQL on SQLite, `CREATE TABLE IF NOT EXISTS` | one small service on one disk is enough for the first contracts; the SQL is portable. Move to Postgres + Alembic when there is more than one writer or a second deployment |
| Redis + Celery/Dramatiq | in-process thread pool with a `jobs` table (`foundry/jobs.py`) | same submit/get interface, no extra service to run on Render; swap when calibration volume needs separate workers |
| Next.js + shadcn/ui workbench | plain HTML/JS pages (`/workbench`, `/finance`) on the shared `common.js` | no build step or second service; the API is versioned, so a Next.js front end can replace the pages later without backend changes |
| S3 / MinIO | traces as JSONL files under `DATA_DIR/traces` | one function to change for object storage when traces outgrow the disk |
| uv/poetry, mypy --strict | pip + requirements.txt, ruff-clean new code | keeps the existing Dockerfile and deploy unchanged |
| Separate `foundry-data-os` repo | `seatrail/foundry/` package | one deploy, shared auth and users, and the existing accounting tasks become foundry tasks for free |

Deployment is unchanged: the app stays at `data.seatrial.ai`, separate from the main seatrial.ai
site.

## Operating notes

- Run code tasks from untrusted models on a host with Docker (`SEATRAIL_SANDBOX_BACKEND=docker`); the
  Render web service itself cannot run containers.
- Schedule `python -m seatrail.foundry.drift` weekly, and `POST /finance/auto-accept` daily.
- Set `SEATRAIL_SECRET_KEY` and `SEATRAIL_GRADER_KEY` in production: they sign delivery links and
  encrypt ground truth.
