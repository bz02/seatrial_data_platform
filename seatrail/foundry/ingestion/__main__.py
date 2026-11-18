"""CLI: python -m seatrail.foundry.ingestion scan <folder> --type LEGAL_MATTER --license CC-BY-4.0 ...
          python -m seatrail.foundry.ingestion scan-git <url> --base <commit> --type REPO ...
          python -m seatrail.foundry.ingestion approve <asset_id>"""
from __future__ import annotations

import argparse
import json

from ..models import SourceAssetIn
from . import compliance_gate, service


def main() -> None:
    ap = argparse.ArgumentParser(prog="seatrail.foundry.ingestion")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("scan", "scan-git"):
        p = sub.add_parser(name)
        p.add_argument("source")
        p.add_argument("--base", default=None)
        p.add_argument("--type", required=True)
        p.add_argument("--license", default="")
        p.add_argument("--ai-training-allowed", action="store_true")
        p.add_argument("--chain-of-title", default="")
        p.add_argument("--client-name", action="append", default=[])
        p.add_argument("--domain-tag", default="")
    a = sub.add_parser("approve")
    a.add_argument("asset_id", type=int)
    args = ap.parse_args()
    if args.cmd == "approve":
        print(json.dumps(compliance_gate.approve(args.asset_id), indent=1, default=str))
        return
    asset = SourceAssetIn(source_type=args.type, raw_uri=args.source, license_type=args.license,
                          ai_training_allowed=args.ai_training_allowed, chain_of_title_doc_uri=args.chain_of_title)
    kw = {"git_url": args.source, "base_commit": args.base} if args.cmd == "scan-git" else {"folder": args.source}
    out = service.ingest(asset, client_names=args.client_name, domain_tag=args.domain_tag, **kw)
    print(json.dumps({k: out[k] for k in ("id", "content_hash", "pii_scan_status", "outcome_leakage_status",
                                          "redactions", "alerts")}, indent=1, default=str))


if __name__ == "__main__":
    main()
