#!/bin/bash
# Run MusePilot from a terminal (same app as inside MusePilot.app).
set -e
cd "$(dirname "$0")"
exec /usr/bin/python3 ./musepilot.py "$@"
