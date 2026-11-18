"""Leakage and junk scrubber: classify every file's role and clean crawler filenames.

Roles: CORE (task material), SUPPORTING (git internals, images), CRAWLER_ARTIFACT (scraper output
such as index.csv or html/), POTENTIAL_LEAKAGE (outcome files: final judgments, opinions,
solutions, answer keys, golden outputs). Crawler artifacts and leakage are quarantined, never copied
into `clean/`.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath

CRAWLER_FILES = {"index.csv", "failed_urls.csv", "seeds.csv", "metadata.json", "crawl_log.txt", "robots.txt"}
CRAWLER_DIRS = {"ocr", "html", "_crawl", "__crawler__"}
LEAKAGE_PATTERNS = ["*final_judgment*", "*final-judgment*", "*finaljudgment*", "*opinion*", "*solution*",
                    "*answer_key*", "*answer-key*", "*answerkey*", "*golden*", "*verdict*", "*settlement_agreement*"]
BOILERPLATE = re.compile(r"(?i)(this document was retrieved by|scraped from|downloaded via .* crawler|"
                         r"confidential - do not distribute|proprietary and confidential)")
PREFIX = re.compile(r"^(?:i\d{3,}-[A-Za-z]+[_.\-]*|main_url_content_\d+_+|\d{4,}[_-](?=[A-Za-z]))")


def role_of(rel: str) -> str:
    p = PurePosixPath(rel)
    parts = [x.lower() for x in p.parts]
    if parts and parts[0] == ".git":
        return "SUPPORTING"
    name = p.name.lower()
    if name in CRAWLER_FILES or any(d in CRAWLER_DIRS for d in parts[:-1]):
        return "CRAWLER_ARTIFACT"
    if any(fnmatch.fnmatch(name, pat) for pat in LEAKAGE_PATTERNS):
        return "POTENTIAL_LEAKAGE"
    if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico"}:
        return "SUPPORTING"
    return "CORE"


def clean_name(rel: str) -> str:
    p = PurePosixPath(rel)
    stem = PREFIX.sub("", p.name) or p.name
    return str(p.with_name(stem))


def clean_names(paths: list[str]) -> dict[str, str]:
    """Deterministic original -> clean path mapping, with numeric suffixes on collisions."""
    out, used = {}, set()
    for rel in sorted(paths):
        c = clean_name(rel)
        if c in used:
            p = PurePosixPath(c)
            i = 2
            while str(p.with_name(f"{p.stem}_{i}{p.suffix}")) in used:
                i += 1
            c = str(p.with_name(f"{p.stem}_{i}{p.suffix}"))
        used.add(c)
        out[rel] = c
    return out


def boilerplate_hits(text: str) -> list[int]:
    return [i for i, line in enumerate(text.splitlines(), 1) if BOILERPLATE.search(line)]
