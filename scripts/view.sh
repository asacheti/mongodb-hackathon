#!/usr/bin/env bash
# Serve the precomputed walkthrough (ui/index.html + ui/demo.json). No Atlas, no LLM, no services needed.
# Regenerate the snapshot with: python -m pmp.snapshot   (reads live alignments from Atlas when present)
cd "$(dirname "$0")/../ui"
PORT=${1:-8000}
echo "PMP walkthrough: http://127.0.0.1:$PORT   (Ctrl-C to stop)"
exec python3 -m http.server "$PORT" --bind 127.0.0.1
