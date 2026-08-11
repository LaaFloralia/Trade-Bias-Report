#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TPL="$REPO_ROOT/.env.tpl"
TOKEN_FILE="$HOME/.config/laa/op-service-token"

command -v op >/dev/null 2>&1 || { echo "error: op not found" >&2; exit 127; }
[ -f "$TPL" ] || { echo "error: .env.tpl not found" >&2; exit 1; }

if [ "${1:-}" = "--batch" ]; then
  shift
  [ -f "$TOKEN_FILE" ] || { echo "error: service token not found" >&2; exit 1; }
  OP_SERVICE_ACCOUNT_TOKEN="$(cat "$TOKEN_FILE")" exec op run --env-file="$TPL" -- "$@"
fi

exec op run --env-file="$TPL" -- "$@"
