"""Persistent run state: ledger, baselines, score history, thinking history."""
from __future__ import annotations

from pathlib import Path

from . import util


class State:
    def __init__(self, state_dir: str | Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        for sub in ("research", "reports", "logs"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.data = util.read_json(self.state_path) or {
            "created_at": util.utc_iso(),
            "baseline": None,
            "ledger": [],
            "score_history": [],
            "streaks": {},
            "thinking_history": [],
            "discovered_indicators": [],
            "resume": {"paused": False, "reason": None, "resumed_at": None},
            "runs": [],
        }

    # -- persistence --------------------------------------------------------
    def save(self) -> None:
        util.write_json(self.state_path, self.data)

    # -- helpers -------------------------------------------------------------
    @property
    def ledger(self) -> list:
        return self.data.setdefault("ledger", [])

    def add_trades(self, trades: list[dict]) -> None:
        self.data["ledger"] = self.ledger + trades

    def set_baseline(self, base: dict) -> None:
        self.data["baseline"] = base

    def record_run(self, run: dict) -> None:
        self.data.setdefault("runs", []).append(run)

    def record_score(self, entry: dict) -> None:
        self.data.setdefault("score_history", []).append(entry)

    def record_thinking(self, change: str, date_str: str | None = None) -> None:
        from datetime import date
        self.data.setdefault("thinking_history", []).append({
            "date": date_str or date.today().isoformat(),
            "change": change,
        })

    def mark_paused(self, reason: str) -> None:
        self.data["resume"] = {"paused": True, "reason": reason, "paused_at": util.utc_iso()}

    def resume(self) -> None:
        self.data["resume"] = {"paused": False, "reason": None, "resumed_at": util.utc_iso()}

    @property
    def paused(self) -> bool:
        return bool(self.data.get("resume", {}).get("paused"))
