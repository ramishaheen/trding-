"""Summarize the latest Freqtrade backtest and store it for the dashboard.

Runs INSIDE the freqtrade container (it has the result files + psycopg). Reads
the most recent backtest result, extracts net-after-fees / profit-factor /
winrate / drawdown / trades, and writes them to system_flags['backtest_summary']
so the dashboard can show the historical result next to the live one.

`extract_summary` is pure (no I/O) and unit-tested; key lookups are defensive
because Freqtrade's result keys vary by version.
"""

from __future__ import annotations

import json
import os
import sys


def extract_summary(data: dict, strategy: str | None = None) -> dict:
    strat_map = data.get("strategy") or {}
    name = strategy if strategy in strat_map else (next(iter(strat_map), None))
    s = strat_map.get(name, {}) if name else {}

    total = s.get("total_trades", s.get("trades", 0)) or 0
    winrate = s.get("winrate")
    if winrate is None and total:
        wins = s.get("wins")
        winrate = (wins / total) if isinstance(wins, (int, float)) else None
    dd = s.get("max_drawdown_account")
    if dd is None:
        dd = s.get("max_drawdown")

    def pct(v):
        return round(v * 100, 2) if isinstance(v, (int, float)) else None

    return {
        "strategy": name,
        "profit_total_pct": pct(s.get("profit_total")),
        "profit_abs": (round(s.get("profit_total_abs"), 2)
                       if isinstance(s.get("profit_total_abs"), (int, float)) else None),
        "profit_factor": s.get("profit_factor"),
        "winrate_pct": (round(winrate * 100, 1) if isinstance(winrate, (int, float)) else None),
        "max_drawdown_pct": pct(dd),
        "trades": total,
        "timeframe": s.get("timeframe"),
        "range": f"{s.get('backtest_start','')} → {s.get('backtest_end','')}".strip(" →"),
    }


def _load_latest(results_dir: str) -> dict | None:
    """Load the most recent backtest result JSON (handles .json and .zip)."""
    import glob
    import zipfile

    pointer = os.path.join(results_dir, ".last_result.json")
    target = None
    if os.path.exists(pointer):
        try:
            target = json.load(open(pointer)).get("latest_backtest")
        except Exception:  # noqa: BLE001
            target = None
    if target:
        target = os.path.join(results_dir, target)
    else:
        cands = sorted(glob.glob(os.path.join(results_dir, "backtest-result-*.json"))
                       + glob.glob(os.path.join(results_dir, "backtest-result-*.zip")),
                       key=os.path.getmtime)
        target = cands[-1] if cands else None
    if not target or not os.path.exists(target):
        return None
    if target.endswith(".zip"):
        with zipfile.ZipFile(target) as z:
            jname = next((n for n in z.namelist() if n.endswith(".json")
                          and "config" not in n and "market_change" not in n), None)
            if not jname:
                return None
            return json.loads(z.read(jname))
    return json.load(open(target))


def main() -> int:
    results_dir = os.environ.get(
        "BACKTEST_RESULTS_DIR", "/freqtrade/user_data/backtest_results")
    data = _load_latest(results_dir)
    if not data:
        print("no backtest result found in", results_dir)
        return 1
    summary = extract_summary(data, os.environ.get("BACKTEST_STRATEGY", "MyStrategy"))
    print("backtest summary:", json.dumps(summary, indent=2))

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set; not storing.")
        return 0
    import psycopg

    with psycopg.connect(dsn, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO system_flags (key, value, reason, updated_at)
            VALUES ('backtest_summary', %s, '', now())
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()
            """,
            (json.dumps(summary),),
        )
        conn.commit()
    print("stored backtest_summary.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
