"""Alerting interface for the Risk Governor.

A clean `send_alert(level, title, message, data)` that today logs and optionally
pushes to Telegram, and can later fan out to email / Discord / WhatsApp /
dashboard. Alert delivery must never raise into the trading path.
"""

from __future__ import annotations

import json
import logging
import os
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger("risk_governor.alerts")


class AlertLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    KILL_SWITCH = "kill_switch"


# Optional extra sinks (e.g. a dashboard pushor). Registered at runtime.
_sinks: list[Callable[[str, str, str, dict], None]] = []


def register_sink(fn: Callable[[str, str, str, dict], None]) -> None:
    _sinks.append(fn)


def _telegram(level: str, title: str, message: str) -> None:
    if os.environ.get("TELEGRAM_ENABLED", "false").lower() != "true":
        return
    token = os.environ.get("TELEGRAM_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        import requests

        emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🔴", "kill_switch": "🛑"}.get(level, "")
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": f"{emoji} {title}\n{message}"},
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram alert failed: %s", exc)


def send_alert(level: str | AlertLevel, title: str, message: str,
               data: Optional[dict[str, Any]] = None) -> None:
    level = level.value if isinstance(level, AlertLevel) else str(level)
    data = data or {}
    payload = {"level": level, "title": title, "message": message, "data": data}

    log_fn = {
        "info": logger.info,
        "warning": logger.warning,
        "critical": logger.critical,
        "kill_switch": logger.critical,
    }.get(level, logger.info)
    log_fn("ALERT %s", json.dumps(payload, default=str))

    try:
        _telegram(level, title, message)
    except Exception:  # noqa: BLE001
        pass
    for sink in _sinks:
        try:
            sink(level, title, message, data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("alert sink failed: %s", exc)
