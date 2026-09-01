"""Autonomous self-improving paper trading agent (Alpaca paper + web research).

Package layout:
  config      - configuration & runtime overrides
  util        - json/datetime/logging helpers
  market      - US market calendar, sessions, holidays
  broker      - Alpaca paper REST client + offline MockBroker
  research    - autonomous data collection (FRED, stooq, RSS, SEC) + research inbox
  indicators  - technical indicators from price bars
  signals     - research -> hypotheses with confidence scores
  trading     - order execution, day-trade close-out, risk sizing
  evaluator   - composite performance score, baseline, pause logic
  learning    - self-improvement loop, playbook + signal tracker updates
  reporting   - run reports
  state       - persistent run state
"""

__version__ = "1.0.0"
