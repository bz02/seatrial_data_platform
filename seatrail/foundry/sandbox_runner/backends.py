"""Sandbox backends.

DockerBackend is the production path: one container per rollout from a pinned profile image,
`--network none` unless allowlisted, CPU/RAM/PID caps, read-only root, all capabilities dropped,
workspace bind-mounted from a fresh host temp dir. LocalBackend runs the same tools as a plain
subprocess with rlimits and a scrubbed environment; it is for development and the test suite and
does not isolate the network. Pick with SEATRAIL_SANDBOX_BACKEND=docker|local.
"""
from __future__ import annotations

import os
import resource
import shutil
import signal
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .tools import Backend, ExecResult

# The only environment an agent process gets. No API keys, no grader key, no tokens.
SAFE_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}

MAX_DELIVERABLE_BYTES = 50 * 1024 * 1024


@dataclass
class Limits:
    cpus: float = 1.0
    memory_mb: int = 1024
    pids: int = 256
    cmd_timeout_s: float = 60.0
    network_allowlist: list[str] = field(default_factory=list)


class _HostDirBackend(Backend):
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="seatrail-ws-"))

    def _abs(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if p != self.root.resolve() and self.root.resolve() not in p.parents:
            raise PermissionError(f"path outside workspace: {rel}")
        return p

    def read_bytes(self, rel: str) -> bytes:
        return self._abs(rel).read_bytes()

    def write_bytes(self, rel: str, data: bytes) -> None:
        p = self._abs(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def list_files(self) -> list[str]:
        root = self.root.resolve()
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and not p.is_symlink())

    def snapshot(self) -> dict[str, bytes]:
        out, total = {}, 0
        for rel in self.list_files():
            if "__pycache__" in rel or rel.startswith(".pytest_cache"):
                continue
            data = self.read_bytes(rel)
            total += len(data)
            if total > MAX_DELIVERABLE_BYTES:
                break
            out[rel] = data
        return out

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def destroy(self) -> None:
        self.stop()
        shutil.rmtree(self.root, ignore_errors=True)


class LocalBackend(_HostDirBackend):
    def __init__(self, limits: Limits | None = None):
        super().__init__()
        self.limits = limits or Limits()

    def _preexec(self):
        mem = self.limits.memory_mb * 1024 * 1024
        cpu = int(self.limits.cmd_timeout_s) + 5

        def apply():
            os.setsid()
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
            resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_DELIVERABLE_BYTES, MAX_DELIVERABLE_BYTES))
        return apply

    def exec(self, argv: list[str], timeout: float) -> ExecResult:
        env = {**SAFE_ENV, "HOME": str(self.root), "TMPDIR": str(self.root)}
        try:
            p = subprocess.Popen(argv, cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, preexec_fn=self._preexec())
        except OSError as e:
            return ExecResult(-1, f"sandbox failed to start the command: {e}", env_error=True)
        try:
            out, _ = p.communicate(timeout=timeout)
            return ExecResult(p.returncode, out.decode("utf-8", "replace"))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, _ = p.communicate()
            return ExecResult(-1, out.decode("utf-8", "replace"), timed_out=True)


class DockerBackend(_HostDirBackend):
    """Container per rollout. Commands go through `docker exec`; files through the bind mount."""

    def __init__(self, image: str, limits: Limits | None = None, docker: str = "docker"):
        super().__init__()
        self.image = image
        self.limits = limits or Limits()
        self.docker = docker
        self.name = f"seatrail-rollout-{uuid.uuid4().hex[:12]}"
        self.started = False

    def run_argv(self) -> list[str]:
        net = os.environ.get("SEATRAIL_EGRESS_NETWORK", "seatrail-egress") if self.limits.network_allowlist else "none"
        return [self.docker, "run", "-d", "--rm", "--name", self.name,
                "--network", net,
                "--cpus", str(self.limits.cpus), "--memory", f"{self.limits.memory_mb}m",
                "--memory-swap", f"{self.limits.memory_mb}m", "--pids-limit", str(self.limits.pids),
                "--read-only", "--tmpfs", "/tmp:rw,size=256m", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--user", "10001:10001",
                "-v", f"{self.root}:/workspace:rw", "-w", "/workspace",
                "--label", "seatrail.role=rollout", self.image, "sleep", "infinity"]

    def exec_argv(self, argv: list[str]) -> list[str]:
        return [self.docker, "exec", "-w", "/workspace", self.name, *argv]

    def start(self) -> None:
        os.chmod(self.root, 0o777)
        r = subprocess.run(self.run_argv(), capture_output=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"docker run failed: {r.stderr.decode(errors='replace')[:500]}")
        self.started = True

    def exec(self, argv: list[str], timeout: float) -> ExecResult:
        if not self.started:
            return ExecResult(-1, "sandbox container is not running", env_error=True)
        try:
            r = subprocess.run(self.exec_argv(["timeout", "-s", "KILL", str(int(timeout)), *argv]),
                               capture_output=True, timeout=timeout + 10)
        except subprocess.TimeoutExpired:
            return ExecResult(-1, "", timed_out=True)
        out = (r.stdout + r.stderr).decode("utf-8", "replace")
        if r.returncode == 137:
            return ExecResult(-1, out, timed_out=True)
        env_error = r.returncode in (125, 126) and "Error response from daemon" in out
        return ExecResult(r.returncode, out, env_error=env_error)

    def stop(self) -> None:
        if self.started:
            subprocess.run([self.docker, "rm", "-f", self.name], capture_output=True, timeout=60)
            self.started = False


def make_backend(profile: str, limits: Limits) -> _HostDirBackend:
    kind = os.environ.get("SEATRAIL_SANDBOX_BACKEND", "local")
    if kind == "docker":
        registry = os.environ.get("SEATRAIL_IMAGE_REGISTRY", "seatrail")
        return DockerBackend(f"{registry}/{profile}:pinned", limits)
    return LocalBackend(limits)
