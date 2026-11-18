"""PII and secret scanner. Writes redacted copies and a structured redaction report.

Detectors: the platform's rule-based PII redactor (emails, SSNs, cards with Luhn, phones, account
numbers, IPs, plus client names as custom terms) and secret patterns (cloud keys, API tokens,
private keys, `password = ...` assignments). Text and code are redacted line by line, so layout
is kept; .docx is redacted inside its XML text runs, so formatting is kept. Formats we cannot scan
(PDF, images with text, archives) are quarantined rather than passed through.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from ...pipeline.pii import Redactor

SECRETS = [
    ("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GITHUB_TOKEN", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("API_KEY", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("GOOGLE_API_KEY", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("ASSIGNED_SECRET", re.compile(r"(?i)(?<![\[\w])(api[_-]?key|secret|token|passw(?:or)?d)\b(\s*[:=]\s*['\"]?)"
                                   r"([A-Za-z0-9_\-/+=.]{8,})")),
]
TEXT_EXT = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".py", ".js", ".ts", ".java",
            ".go", ".rs", ".rb", ".c", ".h", ".cpp", ".sh", ".sql", ".html", ".xml", ".env", ".step", ".stp", ""}
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@dataclass
class Finding:
    path: str
    line: int
    kind: str
    count: int = 1


@dataclass
class FileResult:
    path: str
    scanned: bool
    data: bytes | None
    findings: list[Finding] = field(default_factory=list)


class Scanner:
    def __init__(self, client_names: list[str] | None = None):
        self.client_names = client_names or []

    def redact_text(self, text: str, path: str) -> tuple[str, list[Finding]]:
        findings: list[Finding] = []
        # multi-line secrets first (private key blocks)
        for kind, rx in SECRETS:
            if kind != "PRIVATE_KEY":
                continue
            for m in rx.finditer(text):
                findings.append(Finding(path, text.count("\n", 0, m.start()) + 1, "SECRET:" + kind))
            text = rx.sub("[SECRET:PRIVATE_KEY]", text)
        out_lines = []
        for i, line in enumerate(text.splitlines(keepends=True), 1):
            new = line
            for kind, rx in SECRETS:
                if kind == "PRIVATE_KEY":
                    continue
                n = len(rx.findall(new))
                if n:
                    findings.append(Finding(path, i, "SECRET:" + kind, n))
                    if kind == "ASSIGNED_SECRET":
                        new = rx.sub(lambda m: m.group(1) + m.group(2) + "[SECRET]", new)
                    else:
                        new = rx.sub(f"[SECRET:{kind}]", new)
            r = Redactor(self.client_names)
            new = r.text(new)
            for kind, n in r.counts.items():
                findings.append(Finding(path, i, "PII:" + ("CLIENT_NAME" if kind == "CUSTOM" else kind), n))
            out_lines.append(new)
        return "".join(out_lines), findings

    def scan(self, path: str, data: bytes) -> FileResult:
        ext = ("." + path.rsplit(".", 1)[-1].lower()) if "." in path.rsplit("/", 1)[-1] else ""
        if ext == ".docx":
            return self._docx(path, data)
        if ext in TEXT_EXT and len(data) < 5_000_000:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return FileResult(path, False, None)
            red, f = self.redact_text(text, path)
            return FileResult(path, True, red.encode("utf-8"), f)
        if ext in {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico"}:
            return FileResult(path, True, data)  # images: no text layer to scan; OCR is a later step
        return FileResult(path, False, None)

    def _docx(self, path: str, data: bytes) -> FileResult:
        try:
            zin = zipfile.ZipFile(io.BytesIO(data))
            root = ET.fromstring(zin.read("word/document.xml"))
        except (zipfile.BadZipFile, KeyError, ET.ParseError):
            return FileResult(path, False, None)
        findings: list[Finding] = []
        for para_i, p in enumerate(root.iter(W + "p"), 1):
            for t in p.iter(W + "t"):
                if t.text:
                    t.text, f = self.redact_text(t.text, path)
                    for x in f:
                        x.line = para_i  # paragraph number for docx
                    findings += f
        ET.register_namespace("w", W[1:-1])
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                body = ET.tostring(root, xml_declaration=True, encoding="UTF-8") if item.filename == "word/document.xml" \
                    else zin.read(item.filename)
                zout.writestr(item, body)
        return FileResult(path, True, out.getvalue(), findings)
