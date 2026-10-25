"""PII redaction.

Runs on everything a person typed or that we captured: narration, form inputs, email bodies, URLs.
Rule-based on purpose: it is predictable, auditable and runs fully on-prem. Organizations add their
own sensitive terms (customer names, project code names) through their policy.
"""
from __future__ import annotations

import re

SAFE_EMAIL_DOMAINS = (".example", ".test", ".invalid")

PATTERNS = [
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("PHONE", re.compile(r"(?<!\w)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b")),
    ("ACCOUNT", re.compile(r"\b(?:acct|account|routing|iban)[#:\s]*[A-Z0-9]{6,34}\b", re.I)),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]


def _luhn(digits: str) -> bool:
    d = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(d) <= 19:
        return False
    s = 0
    for i, n in enumerate(reversed(d)):
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        s += n
    return s % 10 == 0


class Redactor:
    def __init__(self, custom_terms: list[str] | None = None):
        terms = [t for t in (custom_terms or []) if t and t.strip()]
        self.custom = re.compile("|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)), re.I) \
            if terms else None
        self.counts: dict[str, int] = {}

    def _hit(self, kind: str) -> str:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        return f"[{kind}]"

    def text(self, s: str) -> str:
        if not s:
            return s
        out = s
        if self.custom:
            out = self.custom.sub(lambda m: self._hit("CUSTOM"), out)
        for kind, rx in PATTERNS:
            def repl(m, kind=kind):
                v = m.group(0)
                if kind == "EMAIL" and v.lower().endswith(SAFE_EMAIL_DOMAINS):
                    return v
                if kind == "CARD" and not _luhn(v):
                    return v
                return self._hit(kind)
            out = rx.sub(repl, out)
        return out

    def url(self, u: str) -> str:
        """Drop query strings, which often carry ids and tokens. Keep short hash routes (#invoices)."""
        if not u:
            return u
        base, _, frag = u.partition("#")
        base = base.split("?", 1)[0]
        if frag and "=" not in frag and "&" not in frag and len(frag) <= 40:
            base += "#" + frag
        return self.text(base)

    def report(self) -> dict:
        return {"redactions": dict(self.counts), "total": sum(self.counts.values())}


def residual_scan(s: str) -> list[str]:
    """Kinds of PII still present after redaction (should be empty)."""
    r = Redactor()
    r.text(s)
    return sorted(r.counts)
