"""Task lifecycle. Every status change goes through `check()`; the API maps TransitionError to 409.

    DRAFT -> SYNTH_CHECK -> CALIBRATING -> SME_REVIEW -> QA_APPROVED -> PACKAGED -> DELIVERED
                                 |              |                                    |      |
                                 v              v                                    v      v
                               REWORK <---------+------------------------------ RETURNED  ACCEPTED
                                 |
                                 +--> SYNTH_CHECK   (fixed task is re-verified, then re-calibrated)

Guards are pure functions of a TaskContext so they are easy to test and the same rules hold for the
API, the calibrator's automatic routing and any future worker.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import LeakageStatus, PiiScanStatus, TaskStatus, Verdict

S = TaskStatus

ALLOWED: dict[TaskStatus, set[TaskStatus]] = {
    S.DRAFT: {S.SYNTH_CHECK},
    S.SYNTH_CHECK: {S.CALIBRATING, S.REWORK},
    S.CALIBRATING: {S.SME_REVIEW, S.REWORK},
    S.SME_REVIEW: {S.QA_APPROVED, S.REWORK},
    S.REWORK: {S.SYNTH_CHECK},
    S.QA_APPROVED: {S.PACKAGED, S.REWORK},
    S.PACKAGED: {S.DELIVERED},
    S.DELIVERED: {S.ACCEPTED, S.RETURNED},
    S.RETURNED: {S.REWORK},
    S.ACCEPTED: set(),
}

# Once a task is at or past QA approval it can be shipped, so its sources must be clean.
SHIPPABLE = {S.QA_APPROVED, S.PACKAGED, S.DELIVERED, S.ACCEPTED}


class TransitionError(Exception):
    """Illegal or unguarded status change. HTTP 409 Conflict."""

    def __init__(self, msg: str, code: str = "illegal_transition"):
        super().__init__(msg)
        self.code = code


@dataclass
class SourceState:
    asset_id: int
    pii: PiiScanStatus
    leakage: LeakageStatus
    ai_training_allowed: bool
    license_type: str
    chain_of_title_doc_uri: str
    gate_approved: bool = False


@dataclass
class TaskContext:
    status: TaskStatus
    version: int
    sanity_passed_for_version: bool = False
    criteria_count: int = 0
    latest_verdict: Verdict | None = None
    verdict_version: int | None = None
    sources: list[SourceState] = field(default_factory=list)
    author_id: int | None = None
    active_reviewer_id: int | None = None
    authoring_problems: list[str] = field(default_factory=list)   # prompt lint errors + rubric rule breaks
    reviewer_pod_clash: bool = False


def source_blockers(sources: list[SourceState]) -> list[str]:
    out = []
    for s in sources:
        if s.leakage == LeakageStatus.FLAGGED:
            out.append(f"source {s.asset_id} has an unresolved outcome-leakage flag")
        if s.pii == PiiScanStatus.BLOCKED:
            out.append(f"source {s.asset_id} is blocked by the PII scan")
    return out


def compliance_blockers(sources: list[SourceState]) -> list[str]:
    """Stricter rule for shipping: every source licensed for AI training with a chain of title,
    and scanned clean (or redacted)."""
    out = source_blockers(sources)
    for s in sources:
        if s.pii == PiiScanStatus.PENDING or s.leakage == LeakageStatus.PENDING:
            out.append(f"source {s.asset_id} has not finished PII/leakage scanning")
        if not s.ai_training_allowed:
            out.append(f"source {s.asset_id} is not licensed for AI training")
        if not s.license_type or not s.chain_of_title_doc_uri:
            out.append(f"source {s.asset_id} has no license / chain-of-title document")
        if not s.gate_approved:
            out.append(f"source {s.asset_id} has not passed the compliance gate")
    return out


def check(ctx: TaskContext, to: TaskStatus, actor_id: int | None = None) -> None:
    """Raise TransitionError unless ctx.status -> to is allowed right now."""
    frm = ctx.status
    if to not in ALLOWED[frm]:
        raise TransitionError(f"cannot move a task from {frm} to {to}")

    if frm == S.DRAFT and to == S.SYNTH_CHECK and not ctx.sanity_passed_for_version:
        raise TransitionError("run the environment sanity check first (golden must score 1.0, empty 0.0)",
                              "sanity_check_required")
    if frm == S.REWORK and to == S.SYNTH_CHECK and not ctx.sanity_passed_for_version:
        raise TransitionError("the reworked version has not passed the sanity check", "sanity_check_required")
    if to == S.SYNTH_CHECK and ctx.authoring_problems:
        raise TransitionError("; ".join(ctx.authoring_problems), "authoring_rules")

    if to == S.CALIBRATING:
        if ctx.criteria_count == 0:
            raise TransitionError("add at least one rubric criterion before calibrating", "rubric_required")
        if ctx.authoring_problems:
            raise TransitionError("; ".join(ctx.authoring_problems), "authoring_rules")
        blockers = source_blockers(ctx.sources)
        if blockers:
            raise TransitionError("; ".join(blockers), "source_blocked")

    if frm == S.CALIBRATING and to == S.SME_REVIEW:
        if ctx.latest_verdict != Verdict.SWEET_SPOT or ctx.verdict_version != ctx.version:
            raise TransitionError("only tasks calibrated into the sweet spot go to SME review "
                                  f"(latest verdict: {ctx.latest_verdict})", "calibration_required")

    if to == S.QA_APPROVED:
        if ctx.active_reviewer_id is None:
            raise TransitionError("assign an SME reviewer first", "reviewer_required")
        if ctx.author_id is not None and ctx.active_reviewer_id == ctx.author_id:
            raise TransitionError("the author cannot approve their own task", "conflict_of_interest")
        if ctx.reviewer_pod_clash:
            raise TransitionError("the reviewer is in the author's pod", "conflict_of_interest")
        if actor_id is not None and ctx.author_id is not None and actor_id == ctx.author_id:
            raise TransitionError("the author cannot approve their own task", "conflict_of_interest")

    if to in SHIPPABLE:
        blockers = compliance_blockers(ctx.sources)
        if blockers:
            raise TransitionError("; ".join(blockers), "source_blocked")
