"""Scheduled drift check, e.g. a weekly Render cron job:

    python -m seatrail.foundry.drift --models claude-opus-5-5,openai:gpt-x --k 4
"""
from __future__ import annotations

import argparse
import json

from . import monitor


def main() -> None:
    ap = argparse.ArgumentParser(description="Re-measure shipped tasks on the current frontier panel")
    ap.add_argument("--models", default="", help="comma-separated panel (default SEATRAIL_DRIFT_MODELS)")
    ap.add_argument("--batch", type=int, default=None)
    ap.add_argument("--k", type=int, default=4)
    ap.add_argument("--no-harden", action="store_true", help="report only; do not create harder evolutions")
    a = ap.parse_args()
    from ..api import _drift_panel
    out = monitor.run(batch_id=a.batch, models=_drift_panel([m for m in a.models.split(",") if m]), k=a.k,
                      harden=not a.no_harden)
    print(json.dumps({k: v for k, v in out.items() if k != "reports"}, indent=2))


if __name__ == "__main__":
    main()
