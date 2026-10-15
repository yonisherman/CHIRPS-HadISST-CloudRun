#!/bin/bash
set -euo pipefail
cd /app
exec python -u scripts/workflow.py --suite "${JOB_MODE:?Set JOB_MODE to chirps or hadisst}" "$@"
