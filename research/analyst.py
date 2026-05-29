"""Performance-analyst agent — the system's self-improvement loop.

On a schedule it: builds the trade journal (experience), computes performance
analytics, asks Claude to review it and propose TESTABLE parameter changes, and
stores the review in `strategy_insights` (accumulating knowledge). It is
RECOMMEND-ONLY: it never trades and never changes settings. Proposals are
validated by walk-forward and applied by the operator.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [analyst] %(message)s")
logger = logging.getLogger("analyst")

_running = True


def _stop(signum, frame):  # noqa: ANN001
    global _running
    logger.info("signal %s — stopping after this cycle", signum)
    _running = False


def review(report: dict, n_trades: int, model: str) -> dict:
    from anthropic import Anthropic
    from analyst_prompts import REVIEW_SCHEMA, SYSTEM_PROMPT, build_user_message

    client = Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=4096,
        system=[{"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}}],
        thinking={"type": "adaptive"},
        output_config={"effort": os.environ.get("LLM_EFFORT", "high"),
                       "format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
        messages=[{"role": "user", "content": build_user_message(report, n_trades)}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    return json.loads(text)


def store_insight(insight: dict, report: dict, n_trades: int, model: str) -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.warning("no DATABASE_URL; insight not stored: %s", insight.get("summary"))
        return
    import psycopg

    with psycopg.connect(dsn, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO strategy_insights
                (trades_analyzed, summary, whats_working, whats_not, hypotheses,
                 metrics, source_model)
            VALUES (%s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s)
            """,
            (n_trades, insight.get("summary", ""),
             json.dumps(insight.get("whats_working", [])),
             json.dumps(insight.get("whats_not", [])),
             json.dumps(insight.get("hypotheses", [])),
             json.dumps(report.get("overall", {})), model),
        )
        conn.commit()
    logger.info("stored insight (%d trades): %s", n_trades, insight.get("summary", "")[:160])


def run_cycle(model: str, min_trades: int) -> None:
    from journal import build_journal
    from analytics import performance_report

    outcomes = build_journal()
    n = len(outcomes)
    if n < min_trades:
        logger.info("only %d closed trades (need >= %d) — not enough to learn yet; skipping",
                    n, min_trades)
        return
    report = performance_report(outcomes)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning("ANTHROPIC_API_KEY not set — computed analytics but skipping LLM review")
        return
    insight = review(report, n, model)
    store_insight(insight, report, n, model)
    # Recommend-only: surface how to validate, never auto-apply.
    logger.info("recommended next step: %s", insight.get("recommended_next_step", ""))
    logger.info("validate any change with walk-forward before applying: ./scripts/walk_forward.sh")


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    interval = int(os.environ.get("ANALYST_INTERVAL_SECONDS", "86400"))  # daily
    min_trades = int(os.environ.get("ANALYST_MIN_TRADES", "20"))
    model = os.environ.get("LLM_MODEL", "claude-opus-4-8")
    logger.info("analyst starting: model=%s interval=%ss min_trades=%d", model, interval, min_trades)

    while _running:
        start = time.time()
        try:
            run_cycle(model, min_trades)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            logger.exception("analyst cycle failed: %s", exc)
        remaining = max(0.0, interval - (time.time() - start))
        slept = 0.0
        while _running and slept < remaining:
            time.sleep(min(2.0, remaining - slept))
            slept += 2.0
    logger.info("analyst stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
