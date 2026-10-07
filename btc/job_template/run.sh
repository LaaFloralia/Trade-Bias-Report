#!/bin/bash
# BTCUSD chart-intel entry (installed by btc/install_job.py). Records even startup failures.
set -u
export BTC_JOB_ROOT=@ROOT@
export BTC_SOURCE_REPO=@SOURCE@
export BTC_PYTHON=@PYTHON@
case "${1:-}" in daily|weekly) TASK_KIND="$1"; shift ;; *) printf 'chart-intel-btcusd: daily or weekly required\n' >&2; exit 2 ;; esac
case " $* " in *' --dry-run '*) exec "$BTC_PYTHON" -B "$BTC_JOB_ROOT/runner.py" "$TASK_KIND" "$@" ;; esac
umask 077
mkdir -p "$BTC_JOB_ROOT/logs" || exit 1
TASK_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
printf '{"entryId":"%s","symbol":"BTCUSD","mode":"%s","stage":"launcher","status":"started","at":"%s"}\n' "$TASK_ID" "$TASK_KIND" "$(date -u +%FT%TZ)" >> "$BTC_JOB_ROOT/logs/launcher.jsonl"
"$BTC_PYTHON" -B "$BTC_JOB_ROOT/runner.py" "$TASK_KIND" "$@"
TASK_EXIT=$?
printf '{"entryId":"%s","symbol":"BTCUSD","mode":"%s","stage":"launcher","status":"finished","exitCode":%d,"at":"%s"}\n' "$TASK_ID" "$TASK_KIND" "$TASK_EXIT" "$(date -u +%FT%TZ)" >> "$BTC_JOB_ROOT/logs/launcher.jsonl"
exit "$TASK_EXIT"
