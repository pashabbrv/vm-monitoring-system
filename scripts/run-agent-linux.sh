#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export SERVER_URL="${SERVER_URL:-http://127.0.0.1:8000}"

if [[ -z "${HOST_ID:-}" ]]; then
  if [[ -r /etc/machine-id ]]; then
    MID="$(head -c 12 /etc/machine-id | tr -d '\n')"
  else
    MID="$(tr -dc 'a-f0-9' </dev/urandom | head -c 12 || echo unknownhost)"
  fi
  H="$(hostname -s 2>/dev/null || hostname | cut -d. -f1)"
  H_LC="$(echo "$H" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-')"
  H_LC="${H_LC%-}"
  export HOST_ID="${H_LC}-${MID}"
fi

export HOST_NAME="${HOST_NAME:-$(hostname)}"
export DISK_USAGE_PATH="${DISK_USAGE_PATH:-/}"

PY="python3"
command -v "$PY" >/dev/null 2>&1 || PY="python"
exec "$PY" -m agent.main
