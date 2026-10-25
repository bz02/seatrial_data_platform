"""Near-duplicate detection on narration with MinHash over word bigrams.

Estimates the Jaccard similarity between two traces' narration. Catches copied or lightly edited
reasoning even in short texts. For large corpora, add LSH banding on the same signatures.
"""
from __future__ import annotations

import hashlib
import random
import re

NUM_PERM = 128
_P = (1 << 61) - 1
_rng = random.Random(1234567)
_PERMS = [(_rng.randrange(1, _P), _rng.randrange(0, _P)) for _ in range(NUM_PERM)]


def _shingles(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9$.]+", text.lower())
    if len(words) < 2:
        return set(words)
    return {words[i] + " " + words[i + 1] for i in range(len(words) - 1)}


def signature(text: str) -> list[int]:
    hashes = [int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big") for s in _shingles(text)]
    if not hashes:
        return [_P] * NUM_PERM
    return [min((a * h + b) % _P for h in hashes) for a, b in _PERMS]


def similarity(sig_a: list[int], sig_b: list[int]) -> float:
    return sum(1 for x, y in zip(sig_a, sig_b) if x == y) / NUM_PERM


def encode(sig: list[int]) -> str:
    return ",".join(format(x, "x") for x in sig)


def decode(s: str) -> list[int]:
    return [int(x, 16) for x in s.split(",")] if s else []


def find_near_duplicates(sig: list[int], others: list[tuple[int, str]], threshold: float) -> list[dict]:
    """others: (record_id, encoded signature). Returns matches with estimated Jaccard >= threshold."""
    out = []
    for rid, enc in others:
        other = decode(enc)
        if len(other) == NUM_PERM:
            sim = similarity(sig, other)
            if sim >= threshold:
                out.append({"record_id": rid, "similarity": round(sim, 3)})
    return out
