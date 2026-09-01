#!/usr/bin/env bash
# Scheduled loop: runs open (09:25 ET) and close (15:50 ET) on weekdays.
# Add to crontab or run with nohup:  nohup ./run_watch.sh > logs/watch.out 2>&1 &
cd "$(dirname "$0")"
mkdir -p logs
echo "A-Trade watch loop starting $(date -u) — weekday 09:25 ET open / 15:50 ET close"
exec python3 -m atrade.cli watch --interval 60 "$@"
