#!/usr/bin/env bash
# Reset the use case, create the collections, and create the Atlas vector index steps_vec_idx.
# Usage: scripts/seed.sh [use_case_id] [--no-wait]
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
USE_CASE="${1:-bnpl_checkout_v1}"
shift $(( $# > 0 ? 1 : 0 ))
exec "$PY" -m pmp.seed --use-case "$USE_CASE" "$@"
