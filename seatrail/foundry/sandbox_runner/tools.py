"""Workspace tools an agent can call, in the two modes the images expose.

    auto            bash, code_exec, view_image, finish
    agentic_native  bash, read, write, edit, glob, grep, view_image, finish

Stdlib only: this file is copied into every pinned image and served by tool_server.py inside the
container. On the host, the same class drives a LocalBackend (dev/tests) or DockerBackend.
"""
from __future__ import annotations

import base64
import fnmatch
import os
from dataclasses import dataclass, field

MAX_OUTPUT = 20_000
WORKSPACE = "/workspace"


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required if required is not None else list(props),
            "additionalProperties": False}


_S = {"type": "string"}
SPECS = {
    "bash": ("Run a shell command in /workspace. Returns exit code and combined output.",
             _obj({"command": _S})),
    "code_exec": ("Run a Python 3 snippet in /workspace and return its output.", _obj({"code": _S})),
    "read": ("Read a text file.", _obj({"path": _S})),
    "write": ("Create or overwrite a text file.", _obj({"path": _S, "content": _S})),
    "edit": ("Replace one exact occurrence of old_str with new_str in a file.",
             _obj({"path": _S, "old_str": _S, "new_str": _S})),
    "glob": ("List files matching a glob pattern, e.g. **/*.py.", _obj({"pattern": _S})),
    "grep": ("Search files for a substring. Returns path:line: text.", _obj({"pattern": _S, "glob": _S}, ["pattern"])),
    "view_image": ("Look at an image file (PNG/JPEG).", _obj({"path": _S})),
    "finish": ("Declare the task finished. Put a one-paragraph summary of what you did in `summary`.",
               _obj({"summary": _S}, [])),
}
MODES = {
    "auto": ["bash", "code_exec", "view_image", "finish"],
    "agentic_native": ["bash", "read", "write", "edit", "glob", "grep", "view_image", "finish"],
}


@dataclass
class ToolResult:
    output: str
    is_error: bool = False
    image_b64: str | None = None
    env_error: bool = False     # the environment broke, not the agent's fault


@dataclass
class ExecResult:
    exit_code: int
    output: str
    timed_out: bool = False
    env_error: bool = False


class Backend:
    """Where commands run and files live. Paths are relative to the workspace root."""

    def exec(self, argv: list[str], timeout: float) -> ExecResult: ...
    def read_bytes(self, rel: str) -> bytes: ...
    def write_bytes(self, rel: str, data: bytes) -> None: ...
    def list_files(self) -> list[str]: ...


def _trim(s: str) -> str:
    return s if len(s) <= MAX_OUTPUT else s[:MAX_OUTPUT] + f"\n... [{len(s) - MAX_OUTPUT} more characters]"


def rel_path(path: str) -> str:
    """Map an agent path (absolute under /workspace or relative) to a safe relative path."""
    p = str(path or "").strip()
    if p.startswith(WORKSPACE + "/"):
        p = p[len(WORKSPACE) + 1:]
    elif p == WORKSPACE:
        p = ""
    if p.startswith("/"):
        raise PermissionError(f"path outside /workspace: {path}")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if ".." in parts:
        raise PermissionError(f"path outside /workspace: {path}")
    return "/".join(parts)


@dataclass
class WorkspaceTools:
    backend: Backend
    mode: str = "agentic_native"
    cmd_timeout: float = 60.0
    finished: bool = False
    summary: str = ""
    reads: list[str] = field(default_factory=list)

    def specs(self) -> list[dict]:
        return [{"name": n, "description": SPECS[n][0], "input_schema": SPECS[n][1]} for n in MODES[self.mode]]

    def call(self, name: str, args: dict) -> ToolResult:
        if name not in MODES[self.mode]:
            return ToolResult(f"unknown tool {name!r} in {self.mode} mode", is_error=True)
        if self.finished:
            return ToolResult("task already finished", is_error=True)
        try:
            return getattr(self, "_" + name)(**(args or {}))
        except TypeError as e:
            return ToolResult(f"bad arguments for {name}: {e}", is_error=True)
        except (PermissionError, FileNotFoundError, IsADirectoryError, UnicodeDecodeError, ValueError) as e:
            return ToolResult(f"{type(e).__name__}: {e}", is_error=True)

    # -- tools ---------------------------------------------------------------
    def _run(self, argv: list[str]) -> ToolResult:
        r = self.backend.exec(argv, self.cmd_timeout)
        if r.timed_out:
            return ToolResult(_trim(r.output) + f"\n[timed out after {self.cmd_timeout:.0f}s]", is_error=True)
        return ToolResult(_trim(f"exit_code={r.exit_code}\n{r.output}"), is_error=r.exit_code != 0,
                          env_error=r.env_error)

    def _bash(self, command: str) -> ToolResult:
        return self._run(["bash", "-c", command])

    def _code_exec(self, code: str) -> ToolResult:
        return self._run(["python3", "-c", code])

    def _read(self, path: str) -> ToolResult:
        rel = rel_path(path)
        text = self.backend.read_bytes(rel).decode("utf-8")
        self.reads.append(rel)
        return ToolResult(_trim(text))

    def _write(self, path: str, content: str) -> ToolResult:
        rel = rel_path(path)
        if not rel:
            raise IsADirectoryError(path)
        self.backend.write_bytes(rel, content.encode("utf-8"))
        return ToolResult(f"wrote {len(content)} characters to {rel}")

    def _edit(self, path: str, old_str: str, new_str: str) -> ToolResult:
        rel = rel_path(path)
        text = self.backend.read_bytes(rel).decode("utf-8")
        n = text.count(old_str)
        if n != 1:
            return ToolResult(f"old_str occurs {n} times in {rel}; it must occur exactly once", is_error=True)
        self.backend.write_bytes(rel, text.replace(old_str, new_str, 1).encode("utf-8"))
        return ToolResult(f"edited {rel}")

    def _glob(self, pattern: str) -> ToolResult:
        pat = rel_path(pattern) if pattern.startswith(WORKSPACE) else pattern
        files = sorted(f for f in self.backend.list_files()
                       if fnmatch.fnmatch(f, pat) or fnmatch.fnmatch(f, pat.replace("**/", "")))
        return ToolResult("\n".join(files) or "(no matches)")

    def _grep(self, pattern: str, glob: str = "*") -> ToolResult:
        hits = []
        for f in sorted(self.backend.list_files()):
            if not fnmatch.fnmatch(f, glob) and not fnmatch.fnmatch(os.path.basename(f), glob):
                continue
            try:
                lines = self.backend.read_bytes(f).decode("utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            self.reads.append(f)
            hits += [f"{f}:{i}: {ln}" for i, ln in enumerate(lines, 1) if pattern in ln]
        return ToolResult(_trim("\n".join(hits)) or "(no matches)")

    def _view_image(self, path: str) -> ToolResult:
        rel = rel_path(path)
        data = self.backend.read_bytes(rel)
        if not (data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8")):
            return ToolResult(f"{rel} is not a PNG or JPEG", is_error=True)
        self.reads.append(rel)
        return ToolResult(f"[image {rel}, {len(data)} bytes]", image_b64=base64.b64encode(data).decode())

    def _finish(self, summary: str = "") -> ToolResult:
        self.finished = True
        self.summary = summary
        return ToolResult("finished")
