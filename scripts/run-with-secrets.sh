#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TPL="$REPO_ROOT/.env.tpl"
TOKEN_FILE="$HOME/.config/laa/op-service-token"

command -v op >/dev/null 2>&1 || { echo "error: op not found" >&2; exit 127; }
[ -f "$TPL" ] || { echo "error: .env.tpl not found" >&2; exit 1; }

if [ "${1:-}" = "--batch" ]; then
  shift
  # 旧方式: トークンファイル（2026-08-11 の 1Password 移行前の残置互換）
  if [ -f "$TOKEN_FILE" ]; then
    OP_SERVICE_ACCOUNT_TOKEN="$(cat "$TOKEN_FILE")" exec op run --env-file="$TPL" -- "$@"
  fi
  # 現行方式: 共通バッチラッパー（サービストークンは Keychain 管理、
  # 2026-08-11 シークレット移行後の標準経路。launchd / Hermes cron 対応）
  BATCH_WRAPPER="$HOME/.config/laa/op-run-batch.sh"
  [ -x "$BATCH_WRAPPER" ] || {
    echo "error: service token not found ($TOKEN_FILE も $BATCH_WRAPPER も無い)" >&2
    exit 1
  }
  exec "$BATCH_WRAPPER" "$TPL" "$@"
fi

exec op run --env-file="$TPL" -- "$@"
