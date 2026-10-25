# SeaTrail

Turn how experts actually work into post-training data for AI agents: every step they take, the
reasoning behind it, and a quality check before it counts. MVP vertical: accounting.

Two products share one core:

| | Enterprise edition | Marketplace edition |
|---|---|---|
| Who records | A company's own employees, on their real work | Paid outside experts, on sandbox tasks |
| Where | Company's internal web apps, via a browser recorder | Our spreadsheet + mail sandbox with synthetic data |
| Ground truth | Employee's outcome label + steward review | Hidden answer key, verified automatically |
| Data owner | The company. Deployed in its network | SeaTrail; sold as datasets to model builders |
| Output | SFT chat + trajectory files for the company's own post-training | Same files, plus RL environments |

Shared core: event-sourced recording → `seatrail.trajectory.v1` records → PII redaction, near-duplicate
detection, reasoning coverage, model rubric grading (Claude, optional) → human review → export.

## Run it

```bash
pip install -r requirements.txt
python scripts/seed_demo.py            # fresh demo database with users, tasks and sample records
uvicorn seatrail.app:app --port 8000
```

Open http://localhost:8000 and use the **Demo user** switcher in the top bar.

| Try this | As | Page |
|---|---|---|
| Solve a sandbox task while narrating, check quality, submit | Dana Brooks (expert) | /expert |
| Approve records, pay task rewards | Platform Admin | /review |
| Package approved records into a dataset; see fees | Platform Admin | /market |
| Buy and download a dataset | Frontier Lab (buyer) | /market |
| Opt in, record in the demo ERP, review/delete steps, submit | Sarah Kim (employee) | /enterprise → /demo-erp |
| Approve captured records | Tom Reyes (steward) | /review |
| Export post-training files | Mei Lin (ML engineer) | /enterprise |
| Edit privacy policy, read audit log, run retention purge | Priya Shah (org admin) | /enterprise |

Model grading uses Claude (`claude-opus-5-5`) when `ANTHROPIC_API_KEY` is set and the org policy
allows it; otherwise a transparent heuristic grader runs and the UI says so. Enterprise orgs default
to grading **off** so no data leaves their network; they can point it at their own gateway.

## Tests

```bash
python -m pytest -q
```

Covers task generators and verifier (40 seeds per template), PII redaction, duplicate detection,
fee settlement, the model-grader call shape, and full API flows for both editions including
permissions, org isolation, consent, host allowlist, masking, step deletion, retention and audit.

## Browser recorder

`recorder/recorder.js` is the recorder. It runs as the content script of a Chrome extension
(`python scripts/build_extension.py`, then load `dist/seatrail-recorder` unpacked) or embedded with a
`<script>` tag in an internal app, as the demo ERP at `/demo-erp` does. It only activates on hosts in
the org's allowlist, masks sensitive fields in the browser, and supports voice think-aloud through the
browser's speech recognition.

## Layout

```
seatrail/
  app.py               HTTP API + static pages
  db.py                SQLite schema and helpers
  config.py            thresholds, fee settings (env overridable)
  sandbox.py           event-sourced sandbox engine and tool specs
  verifier.py          grades final sandbox state against the hidden answer key
  tasks/accounting.py  bank reconciliation, AP three-way match, T&E audit generators
  pipeline/
    convert.py         events → trajectory records → chat SFT / RL env exports
    pii.py             rule-based redaction + org sensitive terms
    dedup.py           MinHash near-duplicate detection on narration
    judge.py           Claude rubric grader with heuristic fallback
    quality.py         assess → submit → review
  enterprise.py        policy, consent, capture ingest, employee edits, retention, exports
  market.py            datasets, orders, take rate, expert revenue share
  demo.py              demo seed data (scripted episodes; never sell or export these)
  static/              web apps
recorder/              browser recorder + extension
tests/
```

## Not production-ready yet

- Auth is a demo user switcher. Replace with SSO (OIDC/SAML) and real sessions.
- Payments are mocked. Wire Stripe (marketplace orders, expert payouts via Connect).
- SQLite → Postgres; exports → object storage with signed URLs; add LSH for dedup at scale.
- Recorder captures DOM events only. Desktop apps (Excel, SAP GUI) need a native agent.
