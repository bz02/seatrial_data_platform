#!/bin/sh
# Container entrypoint. Demo mode seeds sample data on first boot; otherwise just prepares the schema.
set -e
mkdir -p "${SEATRAIL_DATA_DIR:-/data}"
DB="${SEATRAIL_DB:-${SEATRAIL_DATA_DIR:-/data}/seatrail.db}"
if [ ! -f "$DB" ]; then
  if [ "${SEATRAIL_DEMO_MODE:-true}" = "true" ]; then
    python scripts/seed_demo.py --base-url "${SEATRAIL_PUBLIC_URL:-http://localhost:8000}"
  else
    python scripts/bootstrap.py init
  fi
fi
exec uvicorn seatrail.app:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips='*'
