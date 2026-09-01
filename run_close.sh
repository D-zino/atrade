#!/usr/bin/env bash
# Close run: close day trades, evaluate P&L, run self-improvement loop, update playbook
cd "$(dirname "$0")"
exec python3 -m atrade.cli close "$@"
