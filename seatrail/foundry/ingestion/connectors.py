"""Connectors and the content-addressed snapshotter.

Every source is copied into an immutable prefix `sources/{sha256}/raw/` under the object root
(`DATA_DIR/objects`, or an S3/GCS bucket synced from it). The sha256 is over the sorted
(path, file hash) list, so the same folder ingested twice lands in the same place.

Git repos are cut at `base_commit`: every commit, branch, tag and reflog entry after it is removed
before anything is copied, so a future fix commit can never leak into a task workspace.
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ... import config


def object_root() -> Path:
    root = Path(os.environ.get("SEATRAIL_OBJECT_ROOT", str(config.DATA_DIR / "objects")))
    root.mkdir(parents=True, exist_ok=True)
    return root


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def list_tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and not p.is_symlink())


def tree_hash(root: Path) -> tuple[str, dict[str, str]]:
    hashes = {rel: sha256_bytes((root / rel).read_bytes()) for rel in list_tree(root)}
    digest = hashlib.sha256("".join(f"{p}\0{h}\n" for p, h in sorted(hashes.items())).encode()).hexdigest()
    return digest, hashes


def mime_of(rel: str) -> str:
    return mimetypes.guess_type(rel)[0] or "application/octet-stream"


@dataclass
class Snapshot:
    sha256: str
    root: Path                       # sources/{sha256}
    files: dict[str, str]            # rel path -> sha256 (raw)
    stripped_commits: list[str] = field(default_factory=list)
    base_commit: str = ""

    @property
    def raw(self) -> Path:
        return self.root / "raw"


def _commit(staging: Path, stripped: list[str] | None = None, base: str = "") -> Snapshot:
    digest, hashes = tree_hash(staging)
    dest = object_root() / "sources" / digest
    if not (dest / "raw").exists():
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staging, dest / "raw", symlinks=False)
        for p in (dest / "raw").rglob("*"):  # immutable: read-only once written
            if p.is_file():
                p.chmod(0o444)
    return Snapshot(digest, dest, hashes, stripped or [], base)


def snapshot_folder(path: str | Path) -> Snapshot:
    """Local folder (a mounted S3/GCS bucket or a downloaded matter folder works the same way)."""
    src = Path(path)
    if not src.is_dir():
        raise ValueError(f"not a folder: {path}")
    with tempfile.TemporaryDirectory(prefix="seatrail-ingest-") as tmp:
        staging = Path(tmp) / "s"
        shutil.copytree(src, staging, symlinks=False, ignore=shutil.ignore_patterns(".git"))
        return _commit(staging)


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=300,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if r.returncode != 0:
        raise ValueError(f"git {' '.join(args)} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def snapshot_git(url: str, base_commit: str) -> Snapshot:
    """Clone, cut everything after `base_commit`, then snapshot the tree including the trimmed .git."""
    with tempfile.TemporaryDirectory(prefix="seatrail-git-") as tmp:
        repo = Path(tmp) / "repo"
        subprocess.run(["git", "clone", "--quiet", "--no-local", url, str(repo)], check=True, capture_output=True,
                       timeout=600)
        base = _git(repo, "rev-parse", "--verify", f"{base_commit}^{{commit}}").strip()
        everything = set(_git(repo, "rev-list", "--all").split())
        # also count commits that only exist on the remote's other branches
        everything |= set(_git(repo, "rev-list", "--remotes").split())
        kept = set(_git(repo, "rev-list", base).split())
        stripped = sorted(everything - kept)
        _git(repo, "checkout", "--quiet", "--detach", base)
        for ref in _git(repo, "for-each-ref", "--format=%(refname)").split():
            _git(repo, "update-ref", "-d", ref)
        _git(repo, "remote", "remove", "origin") if "origin" in _git(repo, "remote").split() else None
        _git(repo, "checkout", "--quiet", "-B", "base")
        _git(repo, "reflog", "expire", "--expire=now", "--all")
        _git(repo, "gc", "--quiet", "--prune=now", "--aggressive")
        left = set(_git(repo, "rev-list", "--all").split())
        if left - kept:
            raise RuntimeError("future commits survived the cut; refusing to snapshot")
        return _commit(repo, stripped, base)
