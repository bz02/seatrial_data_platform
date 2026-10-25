# SeaTrial

Turn how experts actually work into post-training data for AI agents: every step they take, the
reasoning behind it, and a quality check before it counts. First vertical: accounting.

Two products share one core:

| | Enterprise edition | Expert network & datasets |
|---|---|---|
| Who records | A company's own employees, on their real work | Paid, qualified outside experts, on sandbox tasks |
| Where | Company's internal web apps, via a browser recorder | Spreadsheet + mail sandbox with synthetic company data |
| Ground truth | Outcome label + steward review (two reviewers when quality is low) | Hidden answer key, verified automatically |
| Data owner | The company. Deployed in its network | SeaTrial; sold as datasets or custom orders |
| Extras | Process discovery: workflow maps, variants, coverage, what to record next | Frontier-model baseline per task, model-vs-expert gap, expert tiers |

Shared core: event-sourced recording → `seatrail.trajectory.v1` records → PII redaction,
near-duplicate detection, reasoning coverage, model rubric grading (Claude, optional) → consensus
human review with step-level corrections → export.

**Exports:** `sft_chat.jsonl` (reasoning + tool calls, reviewer fixes applied), `trajectories.jsonl`,
`preferences.jsonl` (original vs. corrected step, for DPO), `rl_environments.jsonl` (initial state +
verifier), `evals.jsonl` (goal + checkpoints from captured work), `datacard.md`, `manifest.json`.

## Run it locally

```bash
pip install -r requirements.txt
python scripts/seed_demo.py            # fresh demo database with users, tasks and sample records
uvicorn seatrail.app:app --port 8000
```

Open http://localhost:8000 for the landing page, or http://localhost:8000/login to pick a demo
account.

| Try this | As | Page |
|---|---|---|
| See tier, take a qualification test, solve a task while narrating | Dana Brooks (expert), or sign up as a new expert | /expert |
| Approve records (two reviewers below 80% quality), fix weak steps | Platform Admin | /review |
| Package datasets, run a model baseline, handle custom requests, see leads and fees | Platform Admin | /market |
| Buy a dataset, order custom data | Frontier Lab (buyer) | /market |
| Opt in, record in the demo ERP, review/delete steps, submit | Sarah Kim (employee) | /enterprise → /demo-erp |
| Process map, coverage and collection priority | Tom Reyes (steward) | /enterprise |
| Export post-training files | Mei Lin (ML engineer) | /enterprise |
| Invite people, edit privacy policy, audit log, retention purge | Priya Shah (org admin) | /enterprise |

Model grading and the baseline use Claude (`claude-opus-5-5`) when `ANTHROPIC_API_KEY` is set and
the org policy allows it; otherwise a transparent heuristic grader runs and the UI says so.
Enterprise orgs default to grading **off** so no data leaves their network; they can point it at
their own gateway. Demo baseline runs are scripted and labeled `demo-scripted-baseline`.

## Deploy

All settings are environment variables; see `.env.example`.

- **Docker / self-hosted (also the enterprise install):** `cp .env.example .env`, edit, `docker compose up -d`.
- **Render:** New → Blueprint → this repo (`render.yaml`, persistent disk at `/data`).
- **Fly.io:** see the comments at the top of `fly.toml`.

`SEATRAIL_DEMO_MODE=true` seeds sample data on first boot and enables one-click demo accounts. For
real users set it to `false`, set `SEATRAIL_SECRET_KEY` (the server refuses to start without it) and
`SEATRAIL_COOKIE_SECURE=true`, then create accounts:

```bash
python scripts/bootstrap.py admin --email you@company.com --name "Your Name"
python scripts/bootstrap.py org --name "Acme" --admin-email it@acme.com --admin-name "IT" --allowed-host erp.acme.internal
```

## Security model

- Password accounts (PBKDF2-SHA256, 310k iterations), server-side sessions in an HttpOnly SameSite
  cookie, login throttling. Experts and buyers self-register; enterprise users are invited.
- CSRF: every state-changing API call needs `X-Requested-With: seatrail` (the recorder's capture
  API uses a per-employee bearer token instead). Security headers and HSTS behind HTTPS.
- Org isolation on every query; employees see only their own captures; every view, approval and
  export is audit-logged.
- Answer keys never leave the server, including in the model baseline's observations.
- Custom-request records are exclusive to the buyer and never enter catalog datasets.

## Tests

```bash
python -m pytest -q
```

32 tests: task generators and verifier, PII redaction, duplicate detection, fee settlement, the
model-grader and baseline-agent call shapes (with a fake client), auth and CSRF, qualification and
retakes, consensus review, corrections → preference export, process discovery, custom requests,
leads, and full API flows for both editions.

## Browser recorder

`recorder/recorder.js` runs as the content script of a Chrome extension
(`python scripts/build_extension.py`, then load `dist/seatrail-recorder` unpacked) or embedded with a
`<script>` tag in an internal app, as the demo ERP at `/demo-erp` does. It only activates on hosts in
the org's allowlist, masks sensitive fields in the browser, and supports voice think-aloud.

## Layout

```
seatrail/
  app.py               HTTP API + pages
  auth.py              passwords, sessions, throttling, invites
  experts.py           qualification tests, retakes, tiers
  db.py / config.py    SQLite schema and helpers / env-overridable settings
  sandbox.py           event-sourced sandbox engine and tool specs
  verifier.py          grades final sandbox state against the hidden answer key
  tasks/accounting.py  bank reconciliation, AP three-way match, T&E audit generators
  pipeline/
    convert.py         events → trajectory records → chat SFT / RL env exports
    pii.py, dedup.py   redaction; MinHash near-duplicates
    judge.py           Claude rubric grader with heuristic fallback
    quality.py         assess → submit → consensus review → corrections
    preferences.py     corrections → corrected SFT + DPO pairs; eval items
    discovery.py       process maps, variants, coverage, collection priority
    agent.py           frontier-model baseline agent and model-vs-expert gap
  enterprise.py        policy, consent, capture ingest, employee edits, retention, exports
  market.py            datasets, orders, fees, expert revenue share, custom requests
  demo.py              demo seed data (scripted; never sell or export these)
  static/              landing page and web apps
recorder/              browser recorder + extension
scripts/               seed_demo, bootstrap, start.sh, build_extension
```

## Still to do before real customers

- SSO (OIDC/SAML) for enterprise; email verification and password reset for self-signup.
- Payments are mocked. Wire Stripe (orders, expert payouts via Connect, tax forms).
- SQLite → Postgres; exports → object storage with signed URLs; LSH for dedup at scale.
- Recorder captures web apps only. Desktop apps (Excel, SAP GUI) need a native agent.
- Terms of service, privacy policy, expert contractor agreement, data processing agreement.
