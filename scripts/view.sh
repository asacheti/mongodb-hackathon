#!/usr/bin/env bash
# Serve the precomputed walkthrough (ui/index.html + ui/demo.js). No Atlas, no LLM, no services needed.
# Usage: scripts/view.sh [port]   (default 8000; if that port is busy the next free one is used)
# Regenerate the data with: python -m pmp.snapshot
cd "$(dirname "$0")/../ui"
PY=../.venv/bin/python; [ -x "$PY" ] || PY=python3
PORT=${1:-8000}
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if "$PY" - "$PORT" <<'PYEOF'
import socket, sys
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", int(sys.argv[1]))); s.close(); sys.exit(0)
except OSError:
    sys.exit(1)
PYEOF
  then break; fi
  echo "port $PORT is busy (another server is still running there); trying $((PORT + 1))"
  PORT=$((PORT + 1))
done
echo "PMP walkthrough: http://127.0.0.1:$PORT   (Ctrl-C to stop)"
exec "$PY" -m http.server "$PORT" --bind 127.0.0.1
