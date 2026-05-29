"""Trade journal builder — the system's growing 'experience'.

Reads CLOSED trades from Freqtrade's (dry-run or live) SQLite DB, joins each with
the market_context that held at entry (regime, risk_state, confidence, sentiment,
per-coin bias), upserts the enriched row into `trade_outcomes`, and returns the
list for the analytics engine. Read-only with respect to trading; best-effort.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3

logger = logging.getLogger("journal")


def _ft_db_path() -> str | None:
    explicit = os.environ.get("FREQTRADE_DB_PATH")
    if explicit and os.path.exists(explicit):
        return explicit
    for cand in (
        "user_data/tradesv3.dryrun.sqlite",
        "/freqtrade/user_data/tradesv3.dryrun.sqlite",
        "/app/user_data/tradesv3.dryrun.sqlite",
        "user_data/tradesv3.sqlite",
        "/freqtrade/user_data/tradesv3.sqlite",
    ):
        if os.path.exists(cand):
            return cand
    return None


def read_closed_trades() -> list[dict]:
    path = _ft_db_path()
    if not path:
        logger.warning("no Freqtrade SQLite DB found (set FREQTRADE_DB_PATH)")
        return []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, pair, open_date, close_date, close_profit, close_profit_abs, "
            "exit_reason FROM trades WHERE is_open = 0"
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("reading Freqtrade trades failed: %s", exc)
        return []


def _hour(ts: str | None) -> int | None:
    if not ts or len(ts) < 13:
        return None
    try:
        return int(ts[11:13])
    except ValueError:
        return None


def _match_context(cur, pair: str, when: str | None) -> dict:
    blank = {"regime": "unknown", "risk_state": "unknown", "confidence": None,
             "sentiment": None, "pair_bias": "unknown"}
    if not when:
        return blank
    try:
        cur.execute(
            "SELECT regime, risk_state, confidence, sentiment, per_pair_bias "
            "FROM market_context WHERE created_at <= %s ORDER BY created_at DESC LIMIT 1",
            (when,),
        )
        r = cur.fetchone()
        if not r:
            return blank
        bias = "unknown"
        ppb = r[4]
        data = ppb if isinstance(ppb, list) else (json.loads(ppb) if ppb else [])
        for item in data:
            if isinstance(item, dict) and str(item.get("pair", "")).upper() == pair.upper():
                bias = str(item.get("bias", "unknown"))
        return {"regime": r[0], "risk_state": r[1], "confidence": r[2],
                "sentiment": r[3], "pair_bias": bias}
    except Exception as exc:  # noqa: BLE001
        logger.info("context match failed for %s: %s", pair, exc)
        return blank


def build_journal() -> list[dict]:
    """Build (and persist) the enriched outcome list. Returns it for analytics."""
    trades = read_closed_trades()
    dsn = os.environ.get("DATABASE_URL")
    if not trades or not dsn:
        return []
    import psycopg

    outcomes: list[dict] = []
    with psycopg.connect(dsn, connect_timeout=5) as conn, conn.cursor() as cur:
        for t in trades:
            ctx = _match_context(cur, t["pair"], t.get("close_date") or t.get("open_date"))
            outcome = {
                "trade_id": t["id"], "pair": t["pair"],
                "profit_ratio": float(t.get("close_profit") or 0.0),
                "profit_abs": float(t.get("close_profit_abs") or 0.0),
                "exit_reason": t.get("exit_reason") or "",
                "hour": _hour(t.get("close_date")),
                **ctx,
            }
            outcomes.append(outcome)
            cur.execute(
                """
                INSERT INTO trade_outcomes
                    (trade_id, pair, open_ts, close_ts, profit_ratio, profit_abs,
                     exit_reason, regime, risk_state, confidence, sentiment, pair_bias)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (trade_id) DO NOTHING
                """,
                (t["id"], t["pair"], t.get("open_date"), t.get("close_date"),
                 outcome["profit_ratio"], outcome["profit_abs"], outcome["exit_reason"],
                 ctx["regime"], ctx["risk_state"], ctx["confidence"], ctx["sentiment"],
                 ctx["pair_bias"]),
            )
        conn.commit()
    logger.info("journal: %d closed trades enriched", len(outcomes))
    return outcomes
