#!/bin/sh
# Build the pinned sandbox images: sh seatrail/foundry/sandbox_runner/images/build.sh [registry]
set -e
REG=${1:-seatrail}
HERE=$(cd "$(dirname "$0")" && pwd)
for p in profile-code-swe profile-doc-office profile-cad-meche; do
  ctx=$(mktemp -d)
  cp "$HERE/$p"/* "$HERE/../tools.py" "$HERE/../tool_server.py" "$ctx/"
  docker build -t "$REG/$p:pinned" "$ctx"
  rm -rf "$ctx"
done
