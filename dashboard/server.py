"""Dashboard aggregation server (FastAPI).

Serves a single-page risk/trading dashboard and a /api/state endpoint that
aggregates:
  * Risk Governor status + Weekly Target object (Postgres system_flags)
  * LLM market_context (Postgres)
  * positions / equity / recent trades (Freqtrade REST API)

Every source is best-effort; when nothing is reachable the endpoint returns a
clearly-labelled DEMO snapshot so the UI is useful out of the box. A prominent
STOP action trips the shared kill switch and calls Freqtrade /stop.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import base64
import secrets

import requests
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="BingX Risk Dashboard")
STATIC = Path(__file__).resolve().parent / "static"

# Optional HTTP Basic Auth. Set DASHBOARD_PASSWORD to protect the dashboard
# (REQUIRED when exposing it on a public server). If unset, no auth (local use).
DASH_USER = os.environ.get("DASHBOARD_USER", "admin")
DASH_PASS = os.environ.get("DASHBOARD_PASSWORD", "")


@app.middleware("http")
async def _basic_auth(request: Request, call_next):
    if DASH_PASS:
        hdr = request.headers.get("authorization", "")
        ok = False
        if hdr.startswith("Basic "):
            try:
                user, pw = base64.b64decode(hdr[6:]).decode().split(":", 1)
                ok = (secrets.compare_digest(user, DASH_USER)
                      and secrets.compare_digest(pw, DASH_PASS))
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return Response("Login required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="Trading Bot"'})
    return await call_next(request)

FREQTRADE_API_URL = os.environ.get("FREQTRADE_API_URL", "http://freqtrade:8080").rstrip("/")
FREQTRADE_AUTH = (os.environ.get("FREQTRADE_USERNAME", "freqtrader"),
                  os.environ.get("FREQTRADE_PASSWORD", ""))
BRIDGE_URL = os.environ.get("EXECUTION_BRIDGE_URL", "http://execution-bridge:8090").rstrip("/")
WEBHOOK_TOKEN = os.environ.get("EXECUTION_WEBHOOK_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL")


def _db_flag(key: str):
    if not DATABASE_URL:
        return None
    try:
        import psycopg
        with psycopg.connect(DATABASE_URL, connect_timeout=3) as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM system_flags WHERE key=%s", (key,))
            row = cur.fetchone()
            return json.loads(row[0]) if row and row[0].startswith("{") else (row[0] if row else None)
    except Exception:
        return None


def _market_context():
    if not DATABASE_URL:
        return None
    try:
        import psycopg
        with psycopg.connect(DATABASE_URL, connect_timeout=3) as conn, conn.cursor() as cur:
            cur.execute("SELECT regime, risk_state, confidence, sentiment, pause_trading, rationale "
                        "FROM market_context ORDER BY created_at DESC LIMIT 1")
            r = cur.fetchone()
            if not r:
                return None
            return {"regime": r[0], "risk_state": r[1], "confidence": r[2],
                    "sentiment": r[3], "pause_trading": r[4], "rationale": r[5]}
    except Exception:
        return None


def _freqtrade(path: str):
    try:
        resp = requests.get(f"{FREQTRADE_API_URL}/api/v1/{path}", auth=FREQTRADE_AUTH, timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _demo_state() -> dict:
    return {
        "demo": True,
        "kill_switch_active": False,
        "armed": False,
        "risk_status": {
            "trading_enabled": True, "risk_mode": "normal", "trading_mode": "REAL_TRADING_STRICT",
            "account_balance": 100.0, "available_margin": 100.0, "daily_pnl": 1.2, "weekly_pnl": 6.5,
            "current_drawdown_percent": 1.1, "equity_peak": 107.0, "consecutive_losses": 0,
            "open_positions": 1, "open_orders": 0, "risk_per_trade_percent": 0.5,
            "max_daily_loss_percent": 2.0, "max_weekly_loss_percent": 5.0,
            "max_total_drawdown_percent": 8.0, "max_leverage": 2, "max_capital_exposure_percent": 15.0,
            "news_pause": False, "last_rejection_reason": "rr 1.20<1.5",
            "last_approved_trade": "BTC/USDT long qty=0.00120000",
            "last_kill_switch_reason": "", "kill_switch_active": False, "manual_restart_required": False,
        },
        "weekly_target": {
            "weekly_start_balance": 100.0, "current_equity": 106.5, "weekly_target_multiplier": 4.0,
            "weekly_target_balance": 400.0, "required_weekly_profit": 300.0,
            "current_weekly_profit": 6.5, "weekly_profit_percent": 6.5,
            "target_completion_percent": 2.17, "remaining_profit_needed": 293.5,
            "remaining_trading_days": 5, "required_daily_return_percent": 38.0,
            "target_status": "unrealistic_under_current_risk_limits", "target_realism": "unrealistic",
            "risk_mode": "normal", "trading_allowed": True, "profit_locked": False,
            "weekly_loss_limit_reached": False, "daily_loss_limit_reached": False, "kill_switch_active": False,
        },
        "market_context": {"regime": "trending_up", "risk_state": "risk_on", "confidence": 0.62,
                           "sentiment": 0.3, "pause_trading": False,
                           "rationale": "Demo: steady uptrend, supportive macro."},
        "positions": [{"pair": "BTC/USDT", "amount": 0.0012, "open_rate": 61000, "profit_pct": 1.4}],
        "trades": [
            {"pair": "BTC/USDT", "profit_pct": 1.4, "open_date": "—", "is_open": True},
            {"pair": "ETH/USDT", "profit_pct": 2.1, "open_date": "—", "is_open": False},
            {"pair": "SOL/USDT", "profit_pct": -0.6, "open_date": "—", "is_open": False},
        ],
        "equity_curve": [100, 100.4, 99.8, 101.2, 102.6, 104.1, 106.5],
        "ts": time.time(),
    }


@app.get("/api/state")
def state() -> JSONResponse:
    risk_status = _db_flag("risk_status")
    weekly = _db_flag("weekly_target")
    mc = _market_context()
    profit = _freqtrade("profit")
    status = _freqtrade("status")
    kill = _db_flag("kill_switch")

    if not any([risk_status, weekly, mc, profit, status]):
        return JSONResponse(_demo_state())

    positions = []
    if isinstance(status, list):
        for t in status:
            positions.append({"pair": t.get("pair"), "amount": t.get("amount"),
                              "open_rate": t.get("open_rate"),
                              "profit_pct": (t.get("profit_ratio") or 0) * 100})
    live = _db_flag("live_enabled")
    return JSONResponse({
        "demo": False,
        "kill_switch_active": (str(kill).lower() in {"on", "true", "1"}) if kill else False,
        "armed": (str(live).lower() in {"on", "true", "1", "armed"}) if live else False,
        "risk_status": risk_status or {},
        "weekly_target": weekly or {},
        "market_context": mc or {},
        "positions": positions,
        "trades": positions,
        "equity_curve": [],
        "ts": time.time(),
    })


@app.post("/api/arm")
def arm() -> dict:
    """Turn real trading ON (operator action). The bot still requires the
    deploy-time master flag + REAL mode, and every trade still passes the Risk
    Governor. Returns the bridge's response."""
    try:
        r = requests.post(f"{BRIDGE_URL}/live/on", headers={"X-Webhook-Token": WEBHOOK_TOKEN}, timeout=5)
        return {"ok": r.ok, "result": r.json() if r.ok else r.text}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@app.post("/api/disarm")
def disarm() -> dict:
    try:
        r = requests.post(f"{BRIDGE_URL}/live/off", headers={"X-Webhook-Token": WEBHOOK_TOKEN}, timeout=5)
        return {"ok": r.ok, "result": r.json() if r.ok else r.text}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@app.post("/api/stop")
def stop() -> dict:
    """Trip the shared kill switch and ask Freqtrade to stop. Best-effort fan-out."""
    results = {}
    try:
        requests.post(f"{BRIDGE_URL}/stop", timeout=5)
        results["bridge"] = "ok"
    except Exception as exc:  # noqa: BLE001
        results["bridge"] = f"err:{exc}"
    try:
        requests.post(f"{FREQTRADE_API_URL}/api/v1/stop", auth=FREQTRADE_AUTH, timeout=5)
        results["freqtrade"] = "ok"
    except Exception as exc:  # noqa: BLE001
        results["freqtrade"] = f"err:{exc}"
    return {"stopped": True, "results": results}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC / "index.html"))


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
