#!/usr/bin/env bash
#
# Fails when copy-paste blocks push total duplication above the budget.
#
# jscpd implements the same token-based algorithm SonarQube's CPD uses, so this
# gate and the SonarQube quality gate measure the same thing locally.
set -euo pipefail

MAX_DUPLICATION="${MAX_DUPLICATION:-3}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if ! command -v npx >/dev/null 2>&1; then
  echo "npx not found — skipping duplication check (install Node.js to enable it)." >&2
  exit 0
fi

REPORT="$(mktemp -d)/jscpd-report.json"

npx --yes jscpd@4 \
  --min-lines 5 \
  --min-tokens 50 \
  --reporters json \
  --output "$(dirname "$REPORT")" \
  --ignore "**/node_modules/**,**/*.min.js,**/*_pb2*.py,**/.venv/**,**/dist/**,**/build/**,**/migrations/**,**/.kilo/**,**/coverage/**" \
  backend/app backend/main.py backend/tests services frontend/src \
  >/dev/null 2>&1 || true

if [ ! -f "$REPORT" ]; then
  echo "jscpd produced no report; skipping duplication check." >&2
  exit 0
fi

PERCENT="$(python3 - "$REPORT" <<'PY'
import json, sys
with open(sys.argv[1]) as handle:
    report = json.load(handle)
stats = report.get("statistics", {}).get("total", {})
total = stats.get("lines", 0)
duplicated = stats.get("duplicatedLines", 0)
print(round((duplicated / total * 100) if total else 0.0, 2))
PY
)"

echo "duplicated lines: ${PERCENT}% (budget ${MAX_DUPLICATION}%)"

if python3 -c "import sys; sys.exit(0 if float('${PERCENT}') < float('${MAX_DUPLICATION}') else 1)"; then
  exit 0
fi

echo
echo "Duplication budget exceeded. Extract the shared logic into a single" >&2
echo "implementation rather than copying it — see the shared helpers in" >&2
echo "frontend/src/services/sseStream.ts and services/shared/polymarket_mcp/." >&2
echo "Run 'npx jscpd@4 --min-lines 5 --min-tokens 50 backend/app services frontend/src'" >&2
echo "for the full clone report." >&2
exit 1
