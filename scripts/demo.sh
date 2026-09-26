#!/usr/bin/env bash
# End-to-end demo: seed -> compile -> align -> merge -> validate -> decide (defaults) -> contract
#                  -> agents A + B, mediator, API in the background -> run 0001 -> run 0002.
# One line per stage with elapsed time. UI at http://127.0.0.1:8000 (kept running until Ctrl-C unless --no-wait).
#
# Usage: scripts/demo.sh [--fixture-align] [--no-wait] [--use-case bnpl_checkout_v1]
#   --fixture-align   skip the LLM: load mock/stages/2_alignments.json (deterministic, no OpenRouter key needed)
#   PMP_ALIGN=fixture has the same effect. If the live aligner fails, the fixture is used automatically.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1   # service logs flush line by line
PY=.venv/bin/python; [ -x "$PY" ] || PY=python3
USE_CASE=bnpl_checkout_v1
ALIGN="${PMP_ALIGN:-live}"
WAIT=1; RUNTIME_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --fixture-align) ALIGN=fixture ;;
    --no-wait) WAIT=0 ;;
    --runtime-only) RUNTIME_ONLY=1 ;;   # skip seed..contract; just start services and drive the runs
    --use-case) USE_CASE="$2"; shift ;;
    *) echo "unknown flag $1"; exit 2 ;;
  esac
  shift
done
API_PORT=${API_PORT:-8000}; A_PORT=${A_PORT:-8001}; B_PORT=${B_PORT:-8002}
A_URL="http://127.0.0.1:$A_PORT"; B_URL="http://127.0.0.1:$B_PORT"; API_URL="http://127.0.0.1:$API_PORT"
LOG_DIR=${LOG_DIR:-/tmp/pmp-demo}; mkdir -p "$LOG_DIR"
PIDS=()
T0=$(date +%s)
elapsed() { printf '%4ds' $(( $(date +%s) - T0 )); }
stage() {  # stage <name> <command...>
  local name="$1"; shift
  local s=$(date +%s)
  if "$@" > "$LOG_DIR/$name.log" 2>&1; then
    printf '[%s] %-10s ok   %3ds   %s\n' "$(elapsed)" "$name" $(( $(date +%s) - s )) "$(tail -1 "$LOG_DIR/$name.log" | cut -c1-110)"
  else
    printf '[%s] %-10s FAIL %3ds   see %s\n' "$(elapsed)" "$name" $(( $(date +%s) - s )) "$LOG_DIR/$name.log"
    tail -5 "$LOG_DIR/$name.log"; return 1
  fi
}
cleanup() {
  for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT INT TERM
wait_port() {  # wait_port <url> <seconds>
  "$PY" - "$1" "$2" <<'PYEOF'
import sys, time, urllib.request
url, secs = sys.argv[1], float(sys.argv[2])
deadline = time.time() + secs
while time.time() < deadline:
    try:
        urllib.request.urlopen(url + "/health", timeout=5).read(); sys.exit(0)
    except Exception:
        time.sleep(0.3)
sys.exit(1)
PYEOF
}

echo "PMP demo: $USE_CASE   (logs in $LOG_DIR; align=$ALIGN)"
if [ "$RUNTIME_ONLY" = "0" ]; then
stage seed     scripts/seed.sh "$USE_CASE"
stage compile  "$PY" -m pmp.compile --use-case "$USE_CASE" --fixture
if [ "$ALIGN" = "fixture" ]; then
  stage align  "$PY" -m pmp.align --use-case "$USE_CASE" --from-fixture
else
  if ! stage align "$PY" -m pmp.align --use-case "$USE_CASE"; then
    echo "        live aligner failed; falling back to the fixture"
    stage align  "$PY" -m pmp.align --use-case "$USE_CASE" --from-fixture
  fi
fi
stage merge    "$PY" -m pmp.merge --use-case "$USE_CASE"
stage validate "$PY" -m pmp.validate --use-case "$USE_CASE" --version 1
stage decide   "$PY" -m pmp.decide --use-case "$USE_CASE" --answer-defaults
stage contract "$PY" -m pmp.contract --use-case "$USE_CASE"
fi

"$PY" -m pmp.runtime.agent --org A --port "$A_PORT" --peer "$B_URL" > "$LOG_DIR/agent-A.log" 2>&1 & PIDS+=($!)
"$PY" -m pmp.runtime.agent --org B --port "$B_PORT" --peer "$A_URL" > "$LOG_DIR/agent-B.log" 2>&1 & PIDS+=($!)
"$PY" -m pmp.runtime.mediator --use-case "$USE_CASE" > "$LOG_DIR/mediator.log" 2>&1 & PIDS+=($!)
"$PY" -m pmp.runtime.api --port "$API_PORT" --a-url "$A_URL" --b-url "$B_URL" --use-case "$USE_CASE" > "$LOG_DIR/api.log" 2>&1 & PIDS+=($!)
s=$(date +%s)
wait_port "$A_URL" 40 && wait_port "$B_URL" 40 && wait_port "$API_URL" 40 \
  && printf '[%s] %-10s ok   %3ds   agent A :%s, agent B :%s, mediator (change stream), api :%s\n' "$(elapsed)" services $(( $(date +%s) - s )) "$A_PORT" "$B_PORT" "$API_PORT" \
  || { echo "services did not come up; see $LOG_DIR"; exit 1; }

stage run-0001 "$PY" -m pmp.runtime.run --run 0001 --http --a-url "$A_URL" --b-url "$B_URL" --use-case "$USE_CASE"
stage run-0002 "$PY" -m pmp.runtime.run --run 0002 --http --a-url "$A_URL" --b-url "$B_URL" --use-case "$USE_CASE"
stage update   "$PY" -m pmp.update --use-case "$USE_CASE" --org A --policy large_order_approval --max-amount 15000
printf '[%s] done: seed -> signed contract -> run 0001 -> rejection -> contract v2 -> run 0002 -> republish -> contract v3\n' "$(elapsed)"
echo "UI: $API_URL"
if [ "$WAIT" = "1" ]; then
  echo "services stay up for the UI; press Ctrl-C to stop"
  wait
fi
