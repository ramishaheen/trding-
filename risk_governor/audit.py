"""Structured JSON audit trail for every risk decision.

One JSON object per event, to a dedicated logger (and optional file via
RG_AUDIT_FILE). Every record carries a timestamp and the event type; callers add
symbol, side, balances, calculated risk, reason, config values used, signal/
execution IDs, and any exchange response.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

_audit_logger = logging.getLogger("risk_governor.audit")

_AUDIT_FILE = os.environ.get("RG_AUDIT_FILE")
if _AUDIT_FILE and not _audit_logger.handlers:
    try:
        handler = logging.FileHandler(_AUDIT_FILE)
        handler.setFormatter(logging.Formatter("%(message)s"))
        _audit_logger.addHandler(handler)
        _audit_logger.setLevel(logging.INFO)
    except Exception:  # noqa: BLE001
        pass


def audit(event_type: str, **fields: Any) -> dict:
    """Emit a structured audit record and return it (useful for tests/DB sinks)."""
    record = {"ts": time.time(), "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "event": event_type}
    record.update(fields)
    try:
        _audit_logger.info(json.dumps(record, default=str))
    except Exception:  # noqa: BLE001
        pass
    return record
