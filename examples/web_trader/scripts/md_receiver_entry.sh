#!/usr/bin/env bash
# Start MD receiver under a heartbeat watchdog.
# If the Python process deadlocks (CTP close/GIL) and stops touching the
# heartbeat file, kill it so Docker restart:unless-stopped can recover.
set -u
pip install -q redis >/tmp/pip-redis.log 2>&1 || true

python run_md_receiver.py &
pid=$!
started=$(date +%s)
hb="${MD_RECEIVER_HEARTBEAT_FILE:-/tmp/md_receiver_heartbeat}"

while kill -0 "$pid" 2>/dev/null; do
  sleep 10
  now=$(date +%s)
  # Grace period for CTP login + first subscribe.
  if [ $((now - started)) -lt 120 ]; then
    continue
  fi
  if ! python -c "
import time
from pathlib import Path
p = Path('${hb}')
assert p.exists() and time.time() - p.stat().st_mtime < 60
"; then
    echo "[MD_RX] heartbeat watchdog: killing hung receiver pid=$pid" >&2
    kill -9 "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    exit 75
  fi
done

wait "$pid"
exit $?
