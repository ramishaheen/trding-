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

import hashlib
import logging
import secrets

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [dashboard] %(message)s")
logger = logging.getLogger("dashboard")
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="BingX Risk Dashboard")
STATIC = Path(__file__).resolve().parent / "static"

# Password login. Set DASHBOARD_PASSWORD to protect the dashboard (REQUIRED when
# exposing it on a public server / subdomain). If unset, no login (local use).
DASH_PASS = os.environ.get("DASHBOARD_PASSWORD", "")
COOKIE = "trade_session"


def _expected_token() -> str:
    secret = os.environ.get("DASHBOARD_COOKIE_SECRET") or DASH_PASS
    return hashlib.sha256(("trade|v1|" + secret).encode()).hexdigest()


@app.middleware("http")
async def _auth(request: Request, call_next):
    if not DASH_PASS:                                   # no password set -> open (local use)
        return await call_next(request)
    path = request.url.path
    if path == "/login" or path.startswith("/static"):
        return await call_next(request)
    tok = request.cookies.get(COOKIE, "")
    if tok and secrets.compare_digest(tok, _expected_token()):
        return await call_next(request)
    if path.startswith("/api"):
        return JSONResponse({"error": "login required"}, status_code=401)
    return Response(status_code=302, headers={"Location": "/login"})


LOGIN_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Trade — Login</title>
<style>
 body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  font-family:system-ui,Segoe UI,Roboto,sans-serif;color:#eef1fa;
  background:radial-gradient(900px 600px at 20% -10%,#1c2658,transparent 55%),linear-gradient(160deg,#0a0e1a,#121a38)}
 .box{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:18px;
  padding:34px 30px;width:320px;backdrop-filter:blur(14px);box-shadow:0 12px 40px rgba(0,0,0,.45);text-align:center}
 h1{font-size:20px;margin:0 0 4px} p{color:#9aa4c2;font-size:13px;margin:0 0 20px}
 input{width:100%;box-sizing:border-box;padding:12px 14px;border-radius:11px;border:1px solid rgba(255,255,255,.12);
  background:rgba(255,255,255,.06);color:#fff;font-size:15px;margin-bottom:12px}
 button{width:100%;padding:12px;border:none;border-radius:11px;font-weight:700;font-size:15px;color:#fff;cursor:pointer;
  background:linear-gradient(135deg,#6aa3ff,#2bd49a)}
 .err{color:#ff6171;font-size:13px;height:16px;margin-top:8px}
</style></head><body>
 <form class=box onsubmit="return go(event)">
  <h1>My Trading Bot</h1><p>Enter the password to continue</p>
  <input id=pw type=password placeholder="Password" autofocus autocomplete=current-password>
  <button>Log in</button><div class=err id=err></div>
 </form>
 <script>
 async function go(e){e.preventDefault();
  const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({password:document.getElementById('pw').value})});
  if(r.ok){location.href='/';} else {document.getElementById('err').textContent='Wrong password';}
  return false;}
 </script></body></html>"""


@app.get("/login")
def login_page() -> Response:
    return Response(LOGIN_HTML, media_type="text/html")


@app.post("/login")
async def login(request: Request) -> JSONResponse:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    pw = str((data or {}).get("password", ""))
    if DASH_PASS and secrets.compare_digest(pw, DASH_PASS):
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, _expected_token(), httponly=True, samesite="lax",
                        max_age=7 * 86400)
        return resp
    return JSONResponse({"ok": False}, status_code=401)

FREQTRADE_API_URL = os.environ.get("FREQTRADE_API_URL", "http://freqtrade:8080").rstrip("/")
# Accept either the dashboard-specific vars or the same FREQTRADE__API_SERVER__*
# vars Freqtrade itself reads, so one password in .env drives both sides.
FREQTRADE_AUTH = (
    os.environ.get("FREQTRADE__API_SERVER__USERNAME")
    or os.environ.get("FREQTRADE_USERNAME", "freqtrader"),
    os.environ.get("FREQTRADE__API_SERVER__PASSWORD")
    or os.environ.get("FREQTRADE_PASSWORD", ""),
)
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
            cur.execute("SELECT regime, risk_state, confidence, sentiment, pause_trading, "
                        "rationale, key_risks, per_pair_bias "
                        "FROM market_context ORDER BY created_at DESC LIMIT 1")
            r = cur.fetchone()
            if not r:
                return None

            def _j(v):
                if v is None:
                    return []
                return v if isinstance(v, (list, dict)) else json.loads(v)

            return {"regime": r[0], "risk_state": r[1], "confidence": r[2],
                    "sentiment": r[3], "pause_trading": r[4], "rationale": r[5],
                    "key_risks": _j(r[6]), "per_pair_bias": _j(r[7])}
    except Exception:
        return None


_LAST_FT_ERROR = ""


def _freqtrade(path: str):
    global _LAST_FT_ERROR
    try:
        resp = requests.get(f"{FREQTRADE_API_URL}/api/v1/{path}", auth=FREQTRADE_AUTH, timeout=5)
        if resp.status_code == 401:
            _LAST_FT_ERROR = "401 unauthorized — dashboard password does not match Freqtrade api_server"
            logger.warning("freqtrade %s -> 401 (check FREQTRADE__API_SERVER__PASSWORD)", path)
            return None
        resp.raise_for_status()
        _LAST_FT_ERROR = ""
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        _LAST_FT_ERROR = f"{type(exc).__name__}: {exc}"
        logger.warning("freqtrade %s unreachable: %s", path, exc)
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
                           "rationale": "Demo: steady uptrend, supportive macro.",
                           "key_risks": ["US CPI print tomorrow", "thin weekend liquidity"],
                           "per_pair_bias": [
                               {"pair": "BTC/USDT", "bias": "bullish", "note": "above 200-day"},
                               {"pair": "ETH/USDT", "bias": "neutral", "note": "range-bound"},
                               {"pair": "SOL/USDT", "bias": "bearish", "note": "lost support"}]},
        "positions": [{"pair": "BTC/USDT", "amount": 0.0012, "open_rate": 61000, "profit_pct": 1.4}],
        "trades": [
            {"pair": "BTC/USDT", "profit_pct": 1.4, "open_date": "—", "is_open": True},
            {"pair": "ETH/USDT", "profit_pct": 2.1, "open_date": "—", "is_open": False},
            {"pair": "SOL/USDT", "profit_pct": -0.6, "open_date": "—", "is_open": False},
        ],
        "equity_curve": [100, 100.4, 99.8, 101.2, 102.6, 104.1, 106.5],
        "performance": {"today_pnl": 1.20, "today_trades": 4, "week_pnl": 6.50,
                        "week_trades": 18, "total_pnl": 6.50, "total_trades": 18,
                        "daily": [{"date": "2026-05-24", "pnl": 0.8, "trades": 3},
                                  {"date": "2026-05-25", "pnl": -0.4, "trades": 2},
                                  {"date": "2026-05-26", "pnl": 1.1, "trades": 4},
                                  {"date": "2026-05-27", "pnl": 2.0, "trades": 3},
                                  {"date": "2026-05-28", "pnl": 1.8, "trades": 2},
                                  {"date": "2026-05-29", "pnl": 1.2, "trades": 4}]},
        "ts": time.time(),
    }


def _paper_status_from_freqtrade(balance, count, positions) -> dict:
    """Synthesize the 'Your money'/safety fields from Freqtrade's dry-run wallet
    when the Risk Governor (executor) isn't running. Uses only stable REST
    fields (balance.total, count.current). Shows real paper numbers in the
    paper phase without keys or the live profile."""
    total = (balance or {}).get("total")
    open_n = (count or {}).get("current") if count else len(positions)
    return {
        "trading_enabled": True, "risk_mode": "normal", "trading_mode": "Paper (dry-run)",
        "account_balance": total or 0, "available_margin": total or 0,
        "daily_pnl": 0, "weekly_pnl": 0, "current_drawdown_percent": 0,
        "equity_peak": total or 0, "consecutive_losses": 0,
        "open_positions": open_n or 0, "open_orders": 0,
        "risk_per_trade_percent": 0.5, "max_daily_loss_percent": 2.0,
        "max_weekly_loss_percent": 5.0, "max_total_drawdown_percent": 8.0,
        "max_leverage": 2, "max_capital_exposure_percent": 15.0, "news_pause": False,
        "last_rejection_reason": "", "last_approved_trade": "",
        "last_kill_switch_reason": "", "kill_switch_active": False,
        "manual_restart_required": False,
    }


def _paper_weekly_from_balance(balance) -> dict:
    total = (balance or {}).get("total") or 0
    return {
        "weekly_start_balance": total, "current_equity": total, "weekly_target_multiplier": 4.0,
        "weekly_target_balance": total * 4, "required_weekly_profit": total * 3,
        "current_weekly_profit": 0, "weekly_profit_percent": 0, "target_completion_percent": 0,
        "remaining_profit_needed": total * 3, "remaining_trading_days": 7,
        "required_daily_return_percent": 0, "target_status": "aspirational",
        "risk_mode": "normal", "trading_allowed": True, "profit_locked": False,
        "weekly_loss_limit_reached": False, "daily_loss_limit_reached": False,
        "kill_switch_active": False,
    }


@app.get("/api/state")
def state() -> JSONResponse:
    risk_status = _db_flag("risk_status")
    weekly = _db_flag("weekly_target")
    mc = _market_context()
    profit = _freqtrade("profit")
    status = _freqtrade("status")
    balance = _freqtrade("balance")
    count = _freqtrade("count")
    kill = _db_flag("kill_switch")

    if not any([risk_status, weekly, mc, profit, status, balance]):
        return JSONResponse(_demo_state())

    positions = []
    if isinstance(status, list):
        for t in status:
            positions.append({"pair": t.get("pair"), "amount": t.get("amount"),
                              "open_rate": t.get("open_rate"),
                              "profit_pct": (t.get("profit_ratio") or 0) * 100, "is_open": True})

    # Closed trades (for "Recent trades" + the after-fees performance panel).
    closed_data = _freqtrade("trades?limit=200")
    closed_list = (closed_data.get("trades") if isinstance(closed_data, dict)
                   else closed_data) or []
    recent_closed = []
    for t in [c for c in closed_list if not c.get("is_open")][-12:][::-1]:
        recent_closed.append({"pair": t.get("pair"),
                              "profit_pct": (t.get("close_profit") or 0) * 100,
                              "is_open": False})

    # Paper-mode fallback: when the governor (executor) isn't producing status,
    # show Freqtrade's dry-run wallet so the money/weekly panels aren't blank.
    if not risk_status:
        risk_status = _paper_status_from_freqtrade(balance, count, positions)
    if not weekly:
        weekly = _paper_weekly_from_balance(balance)

    live = _db_flag("live_enabled")
    return JSONResponse({
        "demo": False,
        "kill_switch_active": (str(kill).lower() in {"on", "true", "1"}) if kill else False,
        "armed": (str(live).lower() in {"on", "true", "1", "armed"}) if live else False,
        "risk_status": risk_status or {},
        "weekly_target": weekly or {},
        "market_context": mc or {},
        "positions": positions,
        "trades": (positions + recent_closed)[:12],   # open first, then recent closed
        "equity_curve": [],
        "performance": _performance(closed_data),
        "ts": time.time(),
        # Diagnostics — visit /api/state to see why a panel is blank.
        "_diag": {
            "freqtrade_reachable": balance is not None or status is not None or profit is not None,
            "freqtrade_wallet_total": (balance or {}).get("total"),
            "freqtrade_last_error": _LAST_FT_ERROR,
            "governor_running": _db_flag("risk_status") is not None,
            "sidecar_has_context": mc is not None,
        },
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


_LAST_PERF_LOG = ""


def _performance(data=None) -> dict:
    """Net P&L AFTER FEES from Freqtrade's closed trades (close_profit_abs already
    nets fees). Today / this week / all-time + a small daily series. Logged once
    per UTC day so there's a record without spamming."""
    global _LAST_PERF_LOG
    from datetime import datetime, timedelta, timezone

    if data is None:
        data = _freqtrade("trades?limit=500")
    trades = data.get("trades") if isinstance(data, dict) else (data if isinstance(data, list) else [])
    today = datetime.now(timezone.utc).date()
    week_start = (today - timedelta(days=6)).isoformat()
    today_s = today.isoformat()

    daily: dict[str, list] = {}
    total_pnl, total_n = 0.0, 0
    for t in trades or []:
        if t.get("is_open"):
            continue
        pnl, cd = t.get("close_profit_abs"), (t.get("close_date") or "")
        if pnl is None or len(cd) < 10:
            continue
        d = cd[:10]
        agg = daily.setdefault(d, [0.0, 0])
        agg[0] += float(pnl); agg[1] += 1
        total_pnl += float(pnl); total_n += 1

    tday = daily.get(today_s, [0.0, 0])
    week_pnl = sum(v[0] for d, v in daily.items() if d >= week_start)
    week_n = sum(v[1] for d, v in daily.items() if d >= week_start)
    series = [{"date": d, "pnl": round(v[0], 2), "trades": v[1]}
              for d, v in sorted(daily.items())][-7:]

    if total_n and today_s != _LAST_PERF_LOG:
        _LAST_PERF_LOG = today_s
        logger.info("PERF after fees — today $%.2f (%d), week $%.2f, all $%.2f",
                    tday[0], tday[1], week_pnl, total_pnl)

    return {"today_pnl": round(tday[0], 2), "today_trades": tday[1],
            "week_pnl": round(week_pnl, 2), "week_trades": week_n,
            "total_pnl": round(total_pnl, 2), "total_trades": total_n, "daily": series}


@app.post("/api/test_trade")
async def test_trade(request: Request) -> JSONResponse:
    """Open a PAPER test trade via Freqtrade's forceenter (dry-run only).
    Refused while real trading is armed — defense in depth alongside the UI
    hiding the button. Freqtrade itself always runs dry_run, so this never
    spends real money; the executor's live path is separate."""
    live = _db_flag("live_enabled")
    if live and str(live).lower() in {"on", "true", "1", "armed"}:
        return JSONResponse(
            {"ok": False, "error": "Disabled while real trading is ON (test trades are paper-only)."},
            status_code=403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    pair = (body or {}).get("pair") or "BTC/USDT"
    try:
        r = requests.post(f"{FREQTRADE_API_URL}/api/v1/forceenter",
                          auth=FREQTRADE_AUTH, json={"pair": pair}, timeout=8)
        try:
            payload = r.json()
        except Exception:  # noqa: BLE001
            payload = r.text
        return JSONResponse({"ok": r.status_code == 200, "status": r.status_code,
                             "result": payload}, status_code=(200 if r.status_code == 200 else 502))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


@app.post("/api/close_trade")
async def close_trade(request: Request) -> JSONResponse:
    """Close PAPER trades via Freqtrade's forceexit (dry-run only). Refused while
    real trading is armed — use the STOP button for the live path. Body may pass
    {"tradeid": <id|"all">}; defaults to closing all open paper trades."""
    live = _db_flag("live_enabled")
    if live and str(live).lower() in {"on", "true", "1", "armed"}:
        return JSONResponse(
            {"ok": False, "error": "Disabled while real trading is ON — use STOP for live."},
            status_code=403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    tradeid = (body or {}).get("tradeid") or "all"
    try:
        r = requests.post(f"{FREQTRADE_API_URL}/api/v1/forceexit",
                          auth=FREQTRADE_AUTH, json={"tradeid": tradeid}, timeout=8)
        try:
            payload = r.json()
        except Exception:  # noqa: BLE001
            payload = r.text
        return JSONResponse({"ok": r.status_code == 200, "status": r.status_code,
                             "result": payload}, status_code=(200 if r.status_code == 200 else 502))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


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
