"""Foundry core domain: the one set of types every module (ingestion, sandbox, calibration, SME
dispatch, synthesis, packaging, ledger, drift) shares.

Pydantic v2 schemas are the API contract. Storage is the existing SQLite layer (see schema.py),
kept as plain SQL so the move to Postgres is a connection change, not a rewrite.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DealType(StrEnum):
    OTS = "OTS"                    # off-the-shelf dataset
    COMMISSIONED = "COMMISSIONED"  # custom order for one buyer
    EVAL = "EVAL"                  # held-out evaluation set


class SourceType(StrEnum):
    REPO = "REPO"
    LEGAL_MATTER = "LEGAL_MATTER"
    CAD_ASSEMBLY = "CAD_ASSEMBLY"
    EHR = "EHR"
    FINANCE = "FINANCE"


class PiiScanStatus(StrEnum):
    PENDING = "PENDING"
    CLEAN = "CLEAN"
    REDACTED = "REDACTED"
    BLOCKED = "BLOCKED"


class LeakageStatus(StrEnum):
    PENDING = "PENDING"
    CLEAN = "CLEAN"
    FLAGGED = "FLAGGED"


class Archetype(StrEnum):
    MASK_RECONSTRUCT = "MASK_RECONSTRUCT"
    MULTI_HOP_FLAW = "MULTI_HOP_FLAW"
    FEATURE_TREE_REBUILD = "FEATURE_TREE_REBUILD"
    BUG_FIX = "BUG_FIX"
    E2E_ENV = "E2E_ENV"


class TaskStatus(StrEnum):
    DRAFT = "DRAFT"
    SYNTH_CHECK = "SYNTH_CHECK"
    CALIBRATING = "CALIBRATING"
    SME_REVIEW = "SME_REVIEW"
    REWORK = "REWORK"
    QA_APPROVED = "QA_APPROVED"
    PACKAGED = "PACKAGED"
    DELIVERED = "DELIVERED"
    ACCEPTED = "ACCEPTED"
    RETURNED = "RETURNED"


class CriterionKind(StrEnum):
    PROGRAMMATIC = "PROGRAMMATIC"            # checked by code
    ATOMIC_JUDGE = "ATOMIC_JUDGE"            # one yes/no question for a model or human judge
    DECOY_NEGATIVE = "DECOY_NEGATIVE"        # doing this is wrong (a trap the task sets)
    EXCLUDED_BOUNDARY = "EXCLUDED_BOUNDARY"  # out of scope; must not be rewarded or penalised


class Verdict(StrEnum):
    TOO_HARD_0PCT = "TOO_HARD_0PCT"
    SWEET_SPOT = "SWEET_SPOT"
    SATURATED_100PCT = "SATURATED_100PCT"


class Profile(StrEnum):
    """Pinned environment images (sandbox_runner/images)."""
    CODE_SWE = "profile-code-swe"
    DOC_OFFICE = "profile-doc-office"
    CAD_MECHE = "profile-cad-meche"
    SHEET_ACCOUNTING = "profile-sheet-accounting"  # the existing spreadsheet/inbox sandbox


class ToolMode(StrEnum):
    AUTO = "auto"                      # bash / code_exec, view_image, finish
    AGENTIC_NATIVE = "agentic_native"  # bash, read, write, edit, glob, grep, view_image


class LedgerKind(StrEnum):
    SME_LABOR = "SME_LABOR"
    COMPUTE = "COMPUTE"
    MODEL_API = "MODEL_API"
    REWORK = "REWORK"
    SOURCE_ACQUISITION = "SOURCE_ACQUISITION"
    WARRANTY_REPLACEMENT = "WARRANTY_REPLACEMENT"


class FailureBucket(StrEnum):
    TOOL_LOOP_STALL = "TOOL_LOOP_STALL"
    MISSING_DELIVERABLE_FILE = "MISSING_DELIVERABLE_FILE"
    PARTIAL_DOC_READ_OMISSION = "PARTIAL_DOC_READ_OMISSION"
    SCHEMA_OR_FORMAT_VIOLATION = "SCHEMA_OR_FORMAT_VIOLATION"
    VERIFY_PRECISION_MISS = "VERIFY_PRECISION_MISS"
    HALLUCINATED_CITATION = "HALLUCINATED_CITATION"
    ENVIRONMENT_ERROR = "ENVIRONMENT_ERROR"


# ---------------------------------------------------------------------------
# API schemas
# ---------------------------------------------------------------------------
class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContractBatchIn(_In):
    client_name: str = Field(min_length=1)
    deal_type: DealType
    domain: str = Field(min_length=1)
    target_pass_rate_min: float = Field(0.15, ge=0, le=1)
    target_pass_rate_max: float = Field(0.70, ge=0, le=1)
    warranty_days: int = Field(60, ge=0)
    price_per_accepted_task_usd: float = Field(0, ge=0)
    priority: int = 0
    quoted_cost_per_task_usd: float = Field(0, ge=0)
    payment_terms_days: int = Field(30, ge=0, le=180)

    @model_validator(mode="after")
    def _band(self):
        if self.target_pass_rate_min > self.target_pass_rate_max:
            raise ValueError("target_pass_rate_min must be <= target_pass_rate_max")
        return self


class SourceAssetIn(_In):
    source_type: SourceType
    raw_uri: str
    content_hash: str = ""
    license_type: str = ""
    ai_training_allowed: bool = False
    chain_of_title_doc_uri: str = ""
    pii_scan_status: PiiScanStatus = PiiScanStatus.PENDING
    outcome_leakage_status: LeakageStatus = LeakageStatus.PENDING
    domain_tag: str = ""
    acquisition_cost_usd: float = Field(0, ge=0)


class EnvSpec(_In):
    """What the agent gets. Everything here is visible to the agent; secrets go in SealedSpec."""
    profile: Profile
    mode: ToolMode = ToolMode.AGENTIC_NATIVE
    instructions: str = ""
    workspace: dict[str, str] = Field(default_factory=dict)   # relative path -> text content
    deliverables: list[str] = Field(default_factory=list)     # paths the grader expects
    required_reads: list[str] = Field(default_factory=list)   # inputs a correct solution must open
    max_steps: int = Field(40, ge=1, le=500)
    wall_clock_s: float = Field(600, gt=0)
    network_allowlist: list[str] = Field(default_factory=list)
    sheet_task_id: int | None = None  # profile-sheet-accounting: id of a row in `tasks`

    @field_validator("workspace")
    @classmethod
    def _safe_paths(cls, v: dict[str, str]):
        for p in v:
            if p.startswith("/") or ".." in p.split("/"):
                raise ValueError(f"workspace path must be relative: {p!r}")
        return v


class SealedSpec(_In):
    """Ground truth, hidden tests and the golden solution. Encrypted at rest; only the grader and
    the sanity check ever decrypt it, and never inside the agent's sandbox."""
    grader: dict[str, Any]                                   # {"type": ..., ...}, see verifiers/
    golden: dict[str, Any] = Field(default_factory=dict)     # {"files": {...}} or {"actions": [...]}


class TaskIn(_In):
    batch_id: int
    domain: str
    archetype: Archetype
    title: str = Field(min_length=1)
    author_id: int | None = None
    source_asset_ids: list[int] = Field(default_factory=list)
    env: EnvSpec
    sealed: SealedSpec
    origin: str = Field("HUMAN", pattern="^(HUMAN|SYNTH|EVOLVED)$")
    parent_task_id: int | None = None


class RubricCriterionIn(_In):
    kind: CriterionKind
    weight: float = Field(1.0, ge=0)
    spec_json: dict[str, Any] = Field(default_factory=dict)
    ground_truth_evidence_ref: str = ""


class DefectTag(StrEnum):
    LEAKAGE = "LEAKAGE"
    BAD_VERIFIER = "BAD_VERIFIER"
    AMBIGUOUS_PROMPT = "AMBIGUOUS_PROMPT"
    TOO_EASY = "TOO_EASY"
    UNSOLVABLE = "UNSOLVABLE"


class ExpertProfileIn(_In):
    domain_tags: list[str] = Field(default_factory=list)
    hourly_rate_usd: float = Field(0, ge=0)
    per_task_rate_usd: float = Field(0, ge=0)
    capacity_cap: int = Field(3, ge=0, le=100)
    pod: str = ""
    active: bool = True


class QaDecisionIn(_In):
    decision: str = Field(pattern="^(APPROVE|RETURN)$")
    checklist: dict[str, bool] = Field(default_factory=dict)   # criterion_id -> passes
    defect_tags: list[DefectTag] = Field(default_factory=list)
    notes: str = ""


class TransitionIn(_In):
    to: TaskStatus
    reason: str = ""


class AssignmentIn(_In):
    author_id: int
    reviewer_id: int
    pod: str = ""


class CalibrationRunIn(_In):
    task_id: int | None = None
    batch_id: int | None = None
    models: list[str] = Field(default_factory=list)
    k: int = Field(4, ge=1, le=64)
    concurrency: int = Field(4, ge=1, le=64)

    @model_validator(mode="after")
    def _target(self):
        if (self.task_id is None) == (self.batch_id is None):
            raise ValueError("give exactly one of task_id or batch_id")
        return self


class CriterionResult(BaseModel):
    criterion_id: str
    passed: bool
    score: float
    detail: str = ""


class GraderResult(BaseModel):
    overall_score: float = Field(ge=0, le=1)
    passed: bool
    sub_metrics: dict[str, float] = Field(default_factory=dict)
    criteria_results: list[CriterionResult] = Field(default_factory=list)
    failure_mode: str | None = None   # missing_deliverable | format | environment | precision | citation


class SynthRunIn(_In):
    asset_id: int
    batch_id: int
    model: str | None = None          # author model; default SEATRAIL_SYNTH_MODEL
    critic_model: str | None = None   # devil's advocate; default SEATRAIL_CRITIC_MODEL
    max_candidates: int = Field(3, ge=1, le=20)
    archetype: Archetype | None = None
    author_id: int | None = None      # SME who will own the drafts
    max_revisions: int = Field(1, ge=0, le=3)


class WarrantyClaimIn(_In):
    task_id: int
    defect_tag: DefectTag
    detail: str = Field(min_length=10)


class ClaimDecisionIn(_In):
    valid: bool
    resolution: str = Field("REPLACED", pattern="^(REPLACED|CREDITED)$")
    note: str = ""
