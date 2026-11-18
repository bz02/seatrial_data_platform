"""P1: ingestion, leakage/junk scrubbing, PII + secret redaction, compliance gate.

Adversarial fixtures: a git repo with a secret token and a future fix commit, a legal matter
folder with signed_final_judgment.pdf and failed_urls.csv, and an unlicensed asset."""
import json
import subprocess
from pathlib import Path

import pytest
from conftest import as_user
from foundry_fixtures import ORIGINAL_DOCX, make_docx

from seatrail.foundry import state_machine as sm
from seatrail.foundry import store
from seatrail.foundry.ingestion import compliance_gate, scrubber, service
from seatrail.foundry.ingestion.privacy_gate import Scanner
from seatrail.foundry.models import SourceAssetIn

TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
LICENSED = dict(license_type="Apache-2.0", ai_training_allowed=True, chain_of_title_doc_uri="s3://legal/cot-001.pdf")


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.example", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t.example", "PATH": "/usr/bin:/bin", "HOME": str(cwd)})


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "upstream"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "app.py").write_text(f'GITHUB_TOKEN = "{TOKEN}"\n\ndef total(xs):\n    return sum(xs) - 1\n')
    (r / "README.md").write_text("Contact maintainer jane.doe@realcorp.com or 415-555-0199.\n")
    git(r, "add", ".")
    git(r, "commit", "-q", "-m", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r, capture_output=True, text=True).stdout.strip()
    (r / "app.py").write_text(f'GITHUB_TOKEN = "{TOKEN}"\n\ndef total(xs):\n    return sum(xs)\n')
    git(r, "commit", "-qam", "FIX: off-by-one in total (the answer)")
    git(r, "tag", "v1.0")
    git(r, "checkout", "-q", "-b", "feature")
    (r / "later.py").write_text("x = 1\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "later work")
    git(r, "checkout", "-q", "main")
    return r, base


def test_git_future_commits_stripped_and_secret_redacted(env, repo):
    r, base = repo
    out = service.ingest(SourceAssetIn(source_type="REPO", raw_uri="git://upstream", **LICENSED),
                         git_url=str(r), base_commit=base, domain_tag="K8S_SRE")
    root = Path(out["storage_uri"])
    # the trimmed repo in raw/ only has the base commit
    log = subprocess.run(["git", "log", "--all", "--format=%s"], cwd=root / "raw", capture_output=True, text=True)
    assert log.stdout.strip() == "base"
    assert "FIX" not in subprocess.run(["git", "reflog", "--all"], cwd=root / "raw", capture_output=True,
                                       text=True).stdout
    tags = subprocess.run(["git", "tag"], cwd=root / "raw", capture_output=True, text=True).stdout
    assert tags.strip() == ""
    fut = next(a for a in out["alerts"] if a["kind"] == "FUTURE_COMMIT")
    assert fut["status"] == "STRIPPED" and len(fut["detail"]["commits"]) == 2
    # working tree is at base, secret and PII redacted in clean/, raw untouched
    clean_app = (root / "clean" / "app.py").read_text()
    assert "sum(xs) - 1" in clean_app and TOKEN not in clean_app and "[SECRET:GITHUB_TOKEN]" in clean_app
    readme = (root / "clean" / "README.md").read_text()
    assert "jane.doe@realcorp.com" not in readme and "415-555-0199" not in readme
    assert TOKEN in (root / "raw" / "app.py").read_text()
    assert not (root / "clean" / ".git").exists()
    report = json.loads((root / "redaction_report.json").read_text())
    assert {"path": "app.py", "line": 1, "kind": "SECRET:GITHUB_TOKEN", "count": 1} in report
    assert TOKEN not in json.dumps(report)
    assert out["pii_scan_status"] == "REDACTED" and out["outcome_leakage_status"] == "CLEAN"
    assert compliance_gate.approve(out["id"])["gate_status"] == "APPROVED"


def _legal_folder(tmp_path) -> Path:
    m = tmp_path / "matter-2291"
    (m / "html").mkdir(parents=True)
    (m / "i0001-SUPPLEMENT_complaint.md").write_text("Plaintiff Acme Holdings alleges breach on 2023-04-01.\n")
    (m / "main_url_content_0___motion.md").write_text("Motion to dismiss filed by counsel for Acme Holdings.\n")
    (m / "signed_final_judgment.pdf").write_bytes(b"%PDF-1.4 judgment for plaintiff")
    (m / "failed_urls.csv").write_text("url,status\nhttps://court.example/x,404\n")
    (m / "html" / "page1.html").write_text("<html>scraped</html>")
    (m / "exhibit_a.docx").write_bytes(make_docx("<w:p><w:r><w:t>SSN 123-45-6789 for Acme Holdings</w:t></w:r></w:p>"))
    return m


def test_legal_matter_leakage_and_crawler_junk_quarantined(env, tmp_path):
    out = service.ingest(SourceAssetIn(source_type="LEGAL_MATTER", raw_uri="matter-2291", **LICENSED),
                         folder=str(_legal_folder(tmp_path)), client_names=["Acme Holdings"])
    root = Path(out["storage_uri"])
    clean = sorted(str(p.relative_to(root / "clean")) for p in (root / "clean").rglob("*") if p.is_file())
    assert clean == ["complaint.md", "exhibit_a.docx", "motion.md"]  # prefixes stripped, junk + leakage out
    assert "Acme Holdings" not in (root / "clean" / "complaint.md").read_text()
    from seatrail.foundry.sandbox_runner.verifiers.docx import parse
    text = parse(root / "clean" / "exhibit_a.docx")["accepted_text"]
    assert "123-45-6789" not in text and "Acme" not in text and "[SSN]" in text
    kinds = {(a["kind"], a["path"], a["status"]) for a in out["alerts"]}
    assert ("LEAKAGE", "signed_final_judgment.pdf", "OPEN") in kinds
    assert ("CRAWLER_ARTIFACT", "failed_urls.csv", "QUARANTINED") in kinds
    assert ("CRAWLER_ARTIFACT", "html/page1.html", "QUARANTINED") in kinds
    manifest = {f["path"]: f for f in json.loads((root / "manifest.json").read_text())["files"]}
    assert manifest["signed_final_judgment.pdf"]["role"] == "POTENTIAL_LEAKAGE"
    assert manifest["i0001-SUPPLEMENT_complaint.md"]["clean_path"] == "complaint.md"
    assert out["outcome_leakage_status"] == "FLAGGED"
    with pytest.raises(sm.TransitionError, match="leakage"):
        compliance_gate.approve(out["id"])
    # confirming the quarantine clears the flag; the judgment never enters clean/
    leak = next(a for a in out["alerts"] if a["kind"] == "LEAKAGE")
    compliance_gate.resolve(leak["id"], env["admin@seatrail.example"]["id"], "final judgment is the outcome")
    assert compliance_gate.approve(out["id"])["gate_status"] == "APPROVED"
    assert not (root / "clean" / "signed_final_judgment.pdf").exists()


def test_waiver_needs_reason_and_restores_file(env, tmp_path):
    m = tmp_path / "m"
    m.mkdir()
    (m / "court_opinion_summary_by_paralegal.md").write_text("Neutral procedural history only.\n")
    out = service.ingest(SourceAssetIn(source_type="LEGAL_MATTER", raw_uri="m", **LICENSED), folder=str(m))
    alert = out["alerts"][0]
    admin = env["admin@seatrail.example"]["id"]
    with pytest.raises(ValueError):
        compliance_gate.waive(alert["id"], admin, "ok")
    w = compliance_gate.waive(alert["id"], admin, "Paralegal summary, reviewed: no outcome stated.")
    assert w["admin_id"] == admin and w["created_at"] > 0
    root = Path(out["storage_uri"])
    assert (root / "clean" / "court_opinion_summary_by_paralegal.md").exists()
    assert compliance_gate.approve(out["id"])["gate_status"] == "APPROVED"


def test_unlicensed_asset_is_blocked(env, tmp_path):
    m = tmp_path / "f"
    m.mkdir()
    (m / "budget.csv").write_text("item,amount\nrent,1200\n")
    out = service.ingest(SourceAssetIn(source_type="FINANCE", raw_uri="f", license_type="proprietary"), folder=str(m))
    with pytest.raises(sm.TransitionError) as e:
        compliance_gate.approve(out["id"])
    assert "AI training" in str(e.value) and "chain-of-title" in str(e.value)
    assert store.get_source_asset(out["id"])["gate_status"] == "PENDING"


def test_content_addressed_and_immutable(env, tmp_path):
    m = tmp_path / "same"
    m.mkdir()
    (m / "a.md").write_text("hello\n")
    a = service.ingest(SourceAssetIn(source_type="FINANCE", raw_uri="x", **LICENSED), folder=str(m))
    b = service.ingest(SourceAssetIn(source_type="FINANCE", raw_uri="y", **LICENSED), folder=str(m))
    assert a["content_hash"] == b["content_hash"] and a["storage_uri"] == b["storage_uri"]
    raw = Path(a["storage_uri"]) / "raw" / "a.md"
    assert raw.stat().st_mode & 0o777 == 0o444  # read-only once written


def test_secret_patterns():
    s = Scanner()
    text = ('aws = "AKIAABCDEFGHIJKLMNOP"\npassword = "hunter2hunter2"\nkey = "sk-ant-' + "x" * 30 + '"\n'
            "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----\n")
    red, findings = s.redact_text(text, "cfg.py")
    for secret in ("AKIAABCDEFGHIJKLMNOP", "hunter2hunter2", "sk-ant-", "MIIabc"):
        assert secret not in red
    assert {f.kind for f in findings} >= {"SECRET:AWS_ACCESS_KEY", "SECRET:ASSIGNED_SECRET", "SECRET:API_KEY",
                                          "SECRET:PRIVATE_KEY"}
    assert red.count("\n") == text.count("\n") - 2  # layout kept except the collapsed key block


def test_scrubber_roles_and_names():
    assert scrubber.role_of("ocr/page1.txt") == "CRAWLER_ARTIFACT"
    assert scrubber.role_of("docs/answer_key.json") == "POTENTIAL_LEAKAGE"
    assert scrubber.role_of(".git/HEAD") == "SUPPORTING"
    assert scrubber.clean_names(["i0002-SUPPLEMENT_a.md", "main_url_content_3___a.md"]) == {
        "i0002-SUPPLEMENT_a.md": "a.md", "main_url_content_3___a.md": "a_2.md"}


def test_ingestion_api(client, env, tmp_path, monkeypatch):
    monkeypatch.setenv("SEATRAIL_INGEST_ROOT", str(tmp_path))
    admin = as_user(env["admin@seatrail.example"])
    folder = _legal_folder(tmp_path)
    r = client.post("/api/v1/ingestion/scan", headers=admin, json={
        "folder": str(folder), "source_type": "LEGAL_MATTER", "raw_uri": "matter", **LICENSED})
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    assert client.post(f"/api/v1/ingestion/{aid}/approve", headers=admin).status_code == 409
    # paths outside the ingest root are refused
    r = client.post("/api/v1/ingestion/scan", headers=admin, json={"folder": "/etc", "source_type": "FINANCE"})
    assert r.status_code == 403
    assert client.get(f"/api/v1/ingestion/{aid}/alerts", headers=admin).json()[0]["asset_id"] == aid


def test_docx_layout_kept(env):
    res = Scanner(["Acme"]).scan("a.docx", ORIGINAL_DOCX)
    from seatrail.foundry.sandbox_runner.verifiers import docx
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".docx") as f:
        f.write(res.data)
        f.flush()
        assert docx.parse(Path(f.name))["paragraphs"] == 3
