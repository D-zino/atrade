#!/usr/bin/env bash
# A-Trade setup + first run
set -euo pipefail
cd "$(dirname "$0")"

echo "▸ A-Trade setup — autonomous paper-trading agent"
echo

# 1) credentials
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from template. It currently uses MOCK mode until you add Alpaca paper keys."
  echo "  → open https://app.alpaca.markets/paper to create a free paper account, then"
  echo "    paste your PAPER API keys into $(pwd)/.env"
else
  echo "✓ .env already present"
fi

# 2) sanity: python
python3 --version >/dev/null 2>&1 || { echo "✗ python3 required"; exit 1; }

# 3) seed playbook skeleton
python3 -m atrade.cli playbook

echo
echo "✓ Setup complete. To enable real paper trading, edit .env with your Alpaca PAPER keys."
echo
echo "Next steps:"
echo "  python3 -m atrade.cli status         # see schedule + market state"
echo "  python3 -m atrade.cli open           # run a research + open run now"
echo "  python3 -m atrade.cli close          # close day trades, evaluate, learn"
echo "  ./run_watch.sh                       # run the scheduled loop (weekdays 9:25/15:50 ET)"
