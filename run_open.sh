#!/usr/bin/env bash
# Open run: research + open new trades on hypotheses >= 60% confidence
cd "$(dirname "$0")"
exec python3 -m atrade.cli open "$@"
