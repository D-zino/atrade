"""Small shared helpers: JSON I/O, dates, logging."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ET = "America/New_York"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_LOG_DIR = None  # set by state/engine bootstrap


def configure_logging(log_dir: str | Path) -> None:
    global _LOG_DIR
    _LOG_DIR = Path(log_dir)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    if _LOG_DIR is not None:
        try:
            with open(_LOG_DIR / "agent.log", "a") as f:
                f.write(line + "\n")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# JSON I/O (atomic-ish writes so a crash never corrupts the ledger)
# ---------------------------------------------------------------------------
def read_json(path: str | Path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        log(f"read_json failed for {path}: {e}", "WARN")
        return default


def write_json(path: str | Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Dates / timezone
# ---------------------------------------------------------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    dt = dt or now_utc()
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def pct_change(new: float, old: float) -> float | None:
    if old in (None, 0):
        return None
    return (new - old) / old


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def fmt_pct(x: float | None, digits: int = 2) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:.{digits}f}%"


def fmt_usd(x: float | None, digits: int = 2) -> str:
    if x is None:
        return "n/a"
    return f"${x:,.{digits}f}"
