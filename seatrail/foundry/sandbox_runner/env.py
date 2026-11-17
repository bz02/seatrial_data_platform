"""Environments: what a rollout acts on. One interface for file workspaces (code, office, CAD) and
the existing spreadsheet/inbox sandbox, so calibration and sanity checks treat them the same."""
from __future__ import annotations

import base64
import json

from ... import db, sandbox as sheet_sandbox
from ..models import EnvSpec, Profile
from .backends import Limits, make_backend
from .tools import ToolResult, WorkspaceTools


def decode_content(v: str) -> bytes:
    """Workspace values are text, or `base64:<data>` for binary files such as .docx or .png."""
    return base64.b64decode(v[7:]) if v.startswith("base64:") else v.encode("utf-8")


class Environment:
    finished: bool = False
    reads: list[str]

    def tools(self) -> list[dict]: ...
    def call(self, name: str, args: dict) -> ToolResult: ...
    def deliverables(self) -> dict[str, bytes]: ...
    def file_list(self) -> list[str]: ...
    def summary(self) -> str: return ""
    def close(self) -> None: ...


class WorkspaceEnv(Environment):
    def __init__(self, spec: EnvSpec, limits: Limits | None = None):
        self.spec = spec
        limits = limits or Limits(network_allowlist=spec.network_allowlist)
        self.backend = make_backend(spec.profile, limits)
        for rel, content in spec.workspace.items():
            self.backend.write_bytes(rel, decode_content(content))
        self._initial = set(self.backend.list_files())
        self.backend.start()
        self.toolset = WorkspaceTools(self.backend, mode=spec.mode, cmd_timeout=limits.cmd_timeout_s)

    @property
    def finished(self):  # type: ignore[override]
        return self.toolset.finished

    @property
    def reads(self):  # type: ignore[override]
        return self.toolset.reads

    def tools(self):
        return self.toolset.specs()

    def call(self, name, args):
        return self.toolset.call(name, args)

    def deliverables(self):
        # Stop the container first: grading never happens while the agent can still act.
        self.backend.stop()
        return self.backend.snapshot()

    def file_list(self):
        return sorted(self._initial)

    def summary(self):
        return self.toolset.summary

    def close(self):
        self.backend.destroy()


def _sheet_tool_specs() -> list[dict]:
    from ...pipeline.agent import tools_for_api
    return tools_for_api()


class SheetEnv(Environment):
    """The accounting spreadsheet + inbox sandbox (seatrail/sandbox.py) as a foundry environment."""

    def __init__(self, spec: EnvSpec):
        task = db.q1("SELECT * FROM tasks WHERE id=?", spec.sheet_task_id)
        if not task:
            raise KeyError(f"sheet task {spec.sheet_task_id} not found")
        self.task = task
        self.state = db.loads(task["initial_state"])
        self.reads = []

    @property
    def finished(self):  # type: ignore[override]
        return bool(self.state.get("submitted"))

    def tools(self):
        return _sheet_tool_specs()

    def call(self, name, args):
        from ...pipeline.agent import observe
        try:
            self.state = sheet_sandbox.apply(self.state, name, dict(args or {}))
        except sheet_sandbox.ActionError as e:
            return ToolResult(str(e), is_error=True)
        if name == "view":
            self.reads.append(args.get("target", ""))
            return ToolResult(observe(self.state, self.task, args.get("target", "")))
        return ToolResult("ok")

    def deliverables(self):
        return {"state.json": json.dumps(self.state).encode()}

    def file_list(self):
        return [f"sheet:{s['name']}" for s in self.state["sheets"]] + [f"email:{m['id']}" for m in self.state["inbox"]]

    def close(self):
        pass


def build_env(spec: EnvSpec | dict) -> Environment:
    spec = spec if isinstance(spec, EnvSpec) else EnvSpec.model_validate(spec)
    if spec.profile == Profile.SHEET_ACCOUNTING:
        return SheetEnv(spec)
    return WorkspaceEnv(spec)


def task_prompt(spec: EnvSpec, env: Environment) -> str:
    if isinstance(env, SheetEnv):
        from ...pipeline.agent import _task_prompt
        return _task_prompt(env.task, env.state)
    files = "\n".join(f"- /workspace/{f}" for f in env.file_list()[:200])
    want = ("\nDeliverables expected at: " + ", ".join(spec.deliverables)) if spec.deliverables else ""
    return f"{spec.instructions}\n\nFiles in /workspace:\n{files}{want}\n\nCall finish when you are done."


SYSTEM_WORKSPACE = """You are working in an isolated sandbox with a /workspace directory. Use the tools to
inspect the files, do the task, write the deliverables where the task says, and call finish. There is
no network access. Be precise: an automated grader checks your work."""
