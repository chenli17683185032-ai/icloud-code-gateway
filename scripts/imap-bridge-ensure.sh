#!/bin/bash
# Host-side watchdog for the existing container's persistent IMAP bridge.
set -euo pipefail

exec 9>/run/lock/icloud-imap-bridge-ensure.lock
flock -n 9 || exit 0

container=icloud-code-gateway-app-1
if ! docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null | grep -qx true; then
  exit 0
fi

running=$(docker exec -i -u app "$container" python - <<'PY'
from pathlib import Path


def bridge_running(proc_root=Path("/proc")):
    """Match the real interpreter/script arguments, never command source text."""
    for process in proc_root.iterdir():
        if not process.name.isdigit():
            continue
        try:
            argv = (process / "cmdline").read_bytes().split(b"\0")
        except (OSError, ProcessLookupError):
            continue
        if len(argv) < 2:
            continue
        interpreter = argv[0].rsplit(b"/", 1)[-1]
        if interpreter not in (b"python", b"python3") and not interpreter.startswith(b"python3."):
            continue
        # This watchdog launches exactly `python /data/run_imap_bridge.py`.
        if argv[1] == b"/data/run_imap_bridge.py":
            return True
    return False


print("yes" if bridge_running() else "no")
PY
)

case "$running" in
  yes) exit 0 ;;
  no) docker exec -d -u app "$container" python /data/run_imap_bridge.py ;;
  *) echo "Unexpected IMAP bridge process check result; refusing to start" >&2; exit 1 ;;
esac
