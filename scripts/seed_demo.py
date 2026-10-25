"""Create a fresh demo database. Usage: python scripts/seed_demo.py [--base-url http://localhost:8000]"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seatrail import config, db, demo  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--base-url", default="http://localhost:8000")
ap.add_argument("--keep", action="store_true", help="keep the existing database")
args = ap.parse_args()

if not args.keep and config.DB_PATH.exists():
    config.DB_PATH.unlink()
db.reset()
print(demo.seed_full(args.base_url))
print(f"Seeded {config.DB_PATH}")
