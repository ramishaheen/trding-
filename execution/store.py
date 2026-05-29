"""Postgres-backed queue + state for the live execution path.

Tables (see research/schema.sql):
  execution_orders   pending/approved orders for the browser subagent
  system_flags       kill switch + live account snapshot

Every reader FAILS CLOSED: if the DB is unreachable, account state is reported
as unknown and the kill switch as tripped, so the gate denies new orders.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from execution_logic import AccountState, Decision

logger = logging.getLogger("store")

KILL_FLAG = "kill_switch"
ACCOUNT_SNAPSHOT = "account_snapshot"


def _dsn() -> Optional[str]:
    return os.environ.get("DATABASE_URL")


def _connect():
    import psycopg  # lazy

    dsn = _dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg.connect(dsn, connect_timeout=5)


# --- orders ----------------------------------------------------------------
def enqueue_order(decision: Decision, stake: float) -> int:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO execution_orders
                (status, action, pair, side, order_type, stake, amount, price, tag)
            VALUES ('pending', %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                decision.action, decision.pair, decision.side, decision.order_type,
                stake, decision.amount, decision.price, decision.tag,
            ),
        )
        order_id = cur.fetchone()[0]
        conn.commit()
    return int(order_id)


def claim_next_pending() -> Optional[dict]:
    """Atomically claim the oldest pending order (FOR UPDATE SKIP LOCKED)."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE execution_orders SET status='claimed', claimed_at=now()
            WHERE id = (
                SELECT id FROM execution_orders WHERE status='pending'
                ORDER BY id ASC FOR UPDATE SKIP LOCKED LIMIT 1
            )
            RETURNING id, action, pair, side, order_type, stake, amount, price, tag
            """
        )
        row = cur.fetchone()
        conn.commit()
    if not row:
        return None
    cols = ["id", "action", "pair", "side", "order_type", "stake", "amount", "price", "tag"]
    return dict(zip(cols, row))


def finish_order(order_id: int, status: str, detail: str = "") -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE execution_orders SET status=%s, detail=%s, finished_at=now() WHERE id=%s",
            (status, detail[:1000], order_id),
        )
        conn.commit()


# --- kill switch -----------------------------------------------------------
def read_kill_switch() -> Optional[str]:
    """Return raw flag value, or None on any failure (interpreted as tripped)."""
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM system_flags WHERE key=%s", (KILL_FLAG,))
            row = cur.fetchone()
            return row[0] if row else "off"  # absent flag defaults to running
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.warning("kill-switch read failed (treated as tripped): %s", exc)
        return None


def set_kill_switch(tripped: bool, reason: str = "") -> None:
    value = "on" if tripped else "off"
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO system_flags (key, value, reason, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,
                reason=EXCLUDED.reason, updated_at=now()
            """,
            (KILL_FLAG, value, reason[:500]),
        )
        conn.commit()


# --- live account snapshot -------------------------------------------------
def write_account_snapshot(state: AccountState) -> None:
    payload = json.dumps({
        "known": state.known,
        "equity": state.equity,
        "open_positions": state.open_positions,
        "day_pnl": state.day_pnl,
        "open_pairs": list(state.open_pairs),
    })
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO system_flags (key, value, reason, updated_at)
            VALUES (%s, %s, '', now())
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()
            """,
            (ACCOUNT_SNAPSHOT, payload),
        )
        conn.commit()


def read_account_state() -> AccountState:
    """Read the latest account snapshot. Unknown on any failure (fail closed)."""
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM system_flags WHERE key=%s", (ACCOUNT_SNAPSHOT,))
            row = cur.fetchone()
            if not row:
                return AccountState(known=False)
            data = json.loads(row[0])
            return AccountState(
                known=bool(data.get("known", False)),
                equity=float(data.get("equity", 0.0)),
                open_positions=int(data.get("open_positions", 0)),
                day_pnl=float(data.get("day_pnl", 0.0)),
                open_pairs=tuple(data.get("open_pairs", [])),
            )
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.warning("account snapshot read failed (treated as unknown): %s", exc)
        return AccountState(known=False)
