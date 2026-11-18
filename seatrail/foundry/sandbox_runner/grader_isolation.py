"""Anti-cheat grader isolation.

CRITICAL INVARIANT: ground truth, hidden tests and grading scripts never exist in plaintext on the
agent's filesystem or in its environment during a rollout.

How we hold it:
1. At rest, a task's SealedSpec is encrypted (Fernet: AES-128-CBC + HMAC-SHA256) with a key the
   agent process never sees. The ciphertext lives in the database, not in the workspace.
2. Grading is host-side and post-rollout: after the agent's sandbox stops, its deliverables are
   copied into a *fresh* grader directory (or grader container), the sealed bundle is decrypted
   there, the grader runs, and the directory is deleted. The agent's sandbox is never reused.
3. The agent's processes get a scrubbed environment (no keys, no tokens; see backends.SAFE_ENV).

tests/test_foundry_sandbox.py runs a malicious agent (`find / -name "*truth*"`, `env`, grep for a
marker in the hidden tests) and asserts none of it reaches the agent.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ... import config

KEY_ENV = "SEATRAIL_GRADER_KEY"


def _key() -> bytes:
    k = os.environ.get(KEY_ENV)
    if k:
        return k.encode()
    # Derive a stable key from the server secret so dev setups work without extra config.
    digest = hashlib.sha256(("seatrail-grader:" + config.SECRET_KEY).encode()).digest()
    return base64.urlsafe_b64encode(digest)


def seal(payload: dict) -> str:
    return Fernet(_key()).encrypt(json.dumps(payload, sort_keys=True).encode()).decode()


def unseal(token: str) -> dict:
    try:
        return json.loads(Fernet(_key()).decrypt(token.encode()))
    except InvalidToken as e:
        raise ValueError("sealed grader bundle cannot be decrypted with this key") from e


@contextmanager
def grader_workdir(deliverables: dict[str, bytes]):
    """A fresh directory holding only the agent's deliverables. Deleted afterwards.

    Created only after the rollout has ended, under a 0700 directory, so even a sandbox that shares
    the host could not have seen it during the rollout.
    """
    root = Path(tempfile.mkdtemp(prefix="seatrail-grade-"))
    os.chmod(root, 0o700)
    try:
        out = root / "deliverables"
        out.mkdir()
        for rel, data in deliverables.items():
            p = (out / rel).resolve()
            if out.resolve() not in p.parents:
                continue  # never let a deliverable path escape the grader dir
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
