"""Small but real foundry tasks used across the foundry tests."""
from __future__ import annotations

import base64
import io
import zipfile

SECRET_MARKER = "ZETA-HIDDEN-ORACLE-7741"

BUGGY = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"
FIXED = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"

HIDDEN_TESTS = f'''# {SECRET_MARKER}
from calc import add, mul


def test_add_fixed():
    assert add(2, 3) == 5


def test_mul_still_works():
    assert mul(3, 4) == 12
'''


def code_task(batch_id: int, mode: str = "agentic_native", **extra) -> dict:
    return {
        "batch_id": batch_id, "domain": "swe", "archetype": "BUG_FIX", "title": "Fix add()",
        "env": {"profile": "profile-code-swe", "mode": mode,
                "instructions": "calc.add returns the wrong result. Fix it without breaking mul.",
                "workspace": {"calc.py": BUGGY, "README.md": "Tiny calculator."},
                "deliverables": ["calc.py"], "required_reads": ["calc.py"], "max_steps": 20, "wall_clock_s": 120},
        "sealed": {"grader": {"type": "pytest", "tests": {"test_hidden.py": HIDDEN_TESTS},
                              "fail_to_pass": ["test_hidden.py::test_add_fixed"],
                              "pass_to_pass": ["test_hidden.py::test_mul_still_works"]},
                   "golden": {"files": {"calc.py": FIXED}}},
        **extra,
    }


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_docx(body_xml: str) -> bytes:
    doc = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<w:document xmlns:w="{W_NS}"><w:body>{body_xml}</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def _p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


ORIGINAL_DOCX = make_docx(_p("Master Services Agreement") + _p("Payment is due within 60 days.") + _p("Signed."))
REDLINED_DOCX = make_docx(
    _p("Master Services Agreement")
    + '<w:p><w:r><w:t xml:space="preserve">Payment is due within </w:t></w:r>'
      '<w:del w:id="1" w:author="SME"><w:r><w:delText>60 days</w:delText></w:r></w:del>'
      '<w:ins w:id="2" w:author="SME"><w:r><w:t>30 days</w:t></w:r></w:ins><w:r><w:t>.</w:t></w:r></w:p>'
    + _p("Signed."))


def b64(data: bytes) -> str:
    return "base64:" + base64.b64encode(data).decode()


def docx_task(batch_id: int) -> dict:
    return {
        "batch_id": batch_id, "domain": "legal", "archetype": "MULTI_HOP_FLAW", "title": "Redline payment terms",
        "env": {"profile": "profile-doc-office", "mode": "agentic_native",
                "instructions": "Change the payment term to 30 days as a tracked change.",
                "workspace": {"contract.docx": b64(ORIGINAL_DOCX)}, "deliverables": ["contract.docx"]},
        "sealed": {"grader": {"type": "docx_tracked_changes", "file": "contract.docx",
                              "expect_insertions": ["30 days"], "expect_deletions": ["60 days"],
                              "min_paragraphs": 3},
                   "golden": {"files": {"contract.docx": b64(REDLINED_DOCX)}}},
    }
