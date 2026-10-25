"""Central settings. Override any value with an environment variable of the same name."""
import os
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


DATA_DIR = Path(_env("SEATRAIL_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
DB_PATH = Path(_env("SEATRAIL_DB", str(DATA_DIR / "seatrail.db")))

# Quality gates
VERIFY_PASS_SCORE = float(_env("SEATRAIL_VERIFY_PASS", "0.9"))   # programmatic check must reach this
MIN_THOUGHT_COVERAGE = float(_env("SEATRAIL_MIN_THOUGHT_COVERAGE", "0.5"))  # share of decisions explained
SUBMIT_MIN_QUALITY = float(_env("SEATRAIL_SUBMIT_MIN_QUALITY", "0.7"))  # composite score to allow submit
NEAR_DUP_JACCARD = float(_env("SEATRAIL_NEAR_DUP_JACCARD", "0.7"))

# Model-assisted grading. Without an API key we fall back to a transparent heuristic grader.
JUDGE_MODEL = _env("SEATRAIL_JUDGE_MODEL", "claude-opus-5-5")
JUDGE_ENABLED = _env("SEATRAIL_JUDGE", "auto")  # auto | on | off

# Marketplace economics
# Phase 1: the platform is the only seller. Experts get a task reward plus a share of every sale.
# Phase 2: open marketplace. Third-party sellers list datasets and the platform keeps TAKE_RATE.
OPEN_MARKETPLACE = _env("SEATRAIL_OPEN_MARKETPLACE", "false").lower() == "true"
TAKE_RATE = float(_env("SEATRAIL_TAKE_RATE", "0.15"))
EXPERT_REVENUE_SHARE = float(_env("SEATRAIL_EXPERT_REVENUE_SHARE", "0.20"))
PLATFORM_SELLER_ID = 0
