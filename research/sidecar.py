"""LLM research sidecar.

Runs on a schedule (default every 2 minutes). Each cycle it:
  1. Pulls recent crypto/macro headlines from free RSS feeds.
  2. Computes a basic market summary (latest price/volatility) — best-effort.
  3. Calls Anthropic (claude-opus-4-8) with a hardened classifier prompt that
     treats all news as untrusted data.
  4. Validates the JSON and writes one row to the `market_context` table.

The sidecar NEVER places orders. It only writes a context signal that the
strategy treats as a soft gate. Failures are logged and the loop continues;
a failed cycle simply means the strategy keeps using the previous context (and
the independent risk watchdog + on-exchange stops are unaffected).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import sys
import time
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [sidecar] %(message)s",
)
logger = logging.getLogger("sidecar")

DEFAULT_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://www.investing.com/rss/news_301.rss",  # cryptocurrency news
]

VALID_REGIMES = {"trending_up", "trending_down", "ranging", "high_vol"}
VALID_RISK = {"risk_on", "risk_off", "neutral"}

_running = True


def _handle_sigterm(signum, frame):  # noqa: ANN001
    global _running
    logger.info("received signal %s, shutting down after current cycle", signum)
    _running = False


def fetch_headlines(max_items: int = 25) -> list[str]:
    """Fetch recent headlines from configured RSS feeds. Best-effort."""
    import feedparser  # lazy import

    feeds_env = os.environ.get("NEWS_RSS_FEEDS", "").strip()
    feeds = [f.strip() for f in feeds_env.split(",") if f.strip()] or DEFAULT_FEEDS

    headlines: list[str] = []
    for url in feeds:
        try:
            parsed = feedparser.parse(url)
            for entry in parsed.entries[:max_items]:
                title = getattr(entry, "title", "").strip()
                if title:
                    # Strip control chars; we keep content otherwise verbatim
                    # because the LLM prompt fences it as untrusted data.
                    headlines.append(re.sub(r"\s+", " ", title)[:280])
        except Exception as exc:  # noqa: BLE001
            logger.warning("feed fetch failed for %s: %s", url, exc)
    # de-dup, keep order
    seen, unique = set(), []
    for h in headlines:
        if h not in seen:
            seen.add(h)
            unique.append(h)
    return unique[: max_items * 2]


def _recent_performance(limit: int = 50):
    """Recent bot performance from the trade journal, so Claude knows how the
    strategy has actually been doing. Best-effort."""
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return None
    try:
        import psycopg
        from analytics import overall

        with psycopg.connect(dsn, connect_timeout=3) as conn, conn.cursor() as cur:
            cur.execute("SELECT profit_ratio FROM trade_outcomes ORDER BY id DESC LIMIT %s",
                        (limit,))
            rows = cur.fetchall()
        if not rows:
            return None
        return overall([{"profit_ratio": (r[0] or 0)} for r in rows])
    except Exception as exc:  # noqa: BLE001
        logger.info("recent performance read skipped: %s", exc)
        return None


def compute_market_summary() -> dict[str, Any]:
    """Trusted market context for the classifier: LIVE multi-timeframe price
    structure (trend/volatility/levels/spread) per coin + recent bot performance.
    Best-effort — any piece that can't be computed is simply omitted, and the
    classifier still works off the headlines."""
    summary: dict[str, Any] = {"note": "trusted live data; pieces may be omitted if unavailable"}

    try:
        from market_data import build_market_data

        structure = build_market_data(_allowlist_pairs())
        if structure:
            summary["market_structure"] = structure
    except Exception as exc:  # noqa: BLE001
        logger.info("market structure skipped: %s", exc)

    perf = _recent_performance()
    if perf:
        summary["recent_bot_performance"] = perf
    return summary


def _allowlist_pairs() -> list[str]:
    raw = os.environ.get("LIVE_PAIR_ALLOWLIST", "BTC/USDT,ETH/USDT,SOL/USDT")
    return [p.strip().upper() for p in raw.split(",") if p.strip()]


def classify(market_summary: dict, headlines: list[str], model: str) -> dict[str, Any]:
    """Call Anthropic (claude-opus-4-8) and return a validated context dict.

    Uses: structured outputs (guaranteed-valid JSON), prompt caching on the
    frozen system prompt (news/stats are volatile and live in the user message,
    so the cached prefix stays intact), and adaptive thinking for better
    reasoning. No sampling params (removed on Opus 4.8). The model only writes a
    context signal — it never places a trade.
    """
    from anthropic import Anthropic
    from prompts import CONTEXT_SCHEMA, SYSTEM_PROMPT, build_user_message

    client = Anthropic()  # reads ANTHROPIC_API_KEY from env
    user_msg = build_user_message(market_summary, headlines, _allowlist_pairs())

    resp = client.messages.create(
        model=model,
        max_tokens=4096,
        # Frozen system prompt as the cache prefix (cache_control breakpoint).
        system=[{"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}}],
        # Let Claude reason adaptively; structured output keeps the result valid.
        thinking={"type": "adaptive"},
        output_config={
            "effort": os.environ.get("LLM_EFFORT", "medium"),  # low|medium|high|max
            "format": {"type": "json_schema", "schema": CONTEXT_SCHEMA},
        },
        messages=[{"role": "user", "content": user_msg}],
    )

    usage = getattr(resp, "usage", None)
    if usage is not None:
        logger.info(
            "claude usage: input=%s cache_read=%s cache_write=%s output=%s",
            getattr(usage, "input_tokens", "?"),
            getattr(usage, "cache_read_input_tokens", 0),
            getattr(usage, "cache_creation_input_tokens", 0),
            getattr(usage, "output_tokens", "?"),
        )

    text = "".join(
        block.text for block in resp.content if getattr(block, "type", "") == "text"
    ).strip()
    return validate_context(text, model)


def validate_context(text: str, model: str) -> dict[str, Any]:
    """Parse and validate the model's JSON output. Raises ValueError on bad data
    so the caller can skip writing a malformed row."""
    # Defensive: strip accidental code fences if present.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", cleaned).strip()
    # Extract the first JSON object.
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON object in model output: {text[:200]!r}")
    data = json.loads(match.group(0))

    regime = str(data.get("regime", "")).lower()
    risk_state = str(data.get("risk_state", "")).lower()
    if regime not in VALID_REGIMES:
        raise ValueError(f"invalid regime: {regime!r}")
    if risk_state not in VALID_RISK:
        raise ValueError(f"invalid risk_state: {risk_state!r}")
    confidence = float(data.get("confidence", 0.0))
    confidence = max(0.0, min(1.0, confidence))
    sentiment = float(data.get("sentiment", 0.0))
    sentiment = max(-1.0, min(1.0, sentiment))
    pause_trading = bool(data.get("pause_trading", False))
    rationale = str(data.get("rationale", ""))[:1000]
    notable = data.get("notable_events", [])
    if not isinstance(notable, list):
        notable = []
    notable = [str(x)[:280] for x in notable][:10]

    key_risks = data.get("key_risks", [])
    if not isinstance(key_risks, list):
        key_risks = []
    key_risks = [str(x)[:280] for x in key_risks][:10]

    per_pair = data.get("per_pair_bias", [])
    if not isinstance(per_pair, list):
        per_pair = []
    cleaned_bias = []
    for item in per_pair[:20]:
        if not isinstance(item, dict):
            continue
        bias = str(item.get("bias", "neutral")).lower()
        if bias not in {"bullish", "bearish", "neutral"}:
            bias = "neutral"
        cleaned_bias.append({
            "pair": str(item.get("pair", ""))[:20],
            "bias": bias,
            "note": str(item.get("note", ""))[:200],
        })

    return {
        "regime": regime,
        "risk_state": risk_state,
        "confidence": confidence,
        "sentiment": sentiment,
        "pause_trading": pause_trading,
        "rationale": rationale,
        "notable_events": notable,
        "key_risks": key_risks,
        "per_pair_bias": cleaned_bias,
        "source_model": model,
    }


def store_context(ctx: dict[str, Any], headlines: list[str]) -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.warning("DATABASE_URL not set; logging context instead of storing: %s", ctx)
        return
    import psycopg

    h = hashlib.sha256("\n".join(headlines).encode()).hexdigest()[:32]
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO market_context
                    (regime, risk_state, confidence, sentiment, pause_trading,
                     rationale, notable_events, key_risks, per_pair_bias,
                     source_model, headlines_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s)
                """,
                (
                    ctx["regime"],
                    ctx["risk_state"],
                    ctx["confidence"],
                    ctx["sentiment"],
                    ctx["pause_trading"],
                    ctx["rationale"],
                    json.dumps(ctx["notable_events"]),
                    json.dumps(ctx["key_risks"]),
                    json.dumps(ctx["per_pair_bias"]),
                    ctx["source_model"],
                    h,
                ),
            )
        conn.commit()
    logger.info(
        "stored market_context: regime=%s risk_state=%s confidence=%.2f",
        ctx["regime"], ctx["risk_state"], ctx["confidence"],
    )


def run_cycle(model: str) -> None:
    headlines = fetch_headlines()
    summary = compute_market_summary()
    logger.info("cycle: %d headlines fetched", len(headlines))
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning("ANTHROPIC_API_KEY not set; skipping classification this cycle")
        return
    ctx = classify(summary, headlines, model)
    store_context(ctx, headlines)


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    interval = int(os.environ.get("SIDECAR_INTERVAL_SECONDS", "120"))
    model = os.environ.get("LLM_MODEL", "claude-opus-4-8")
    logger.info("sidecar starting: model=%s interval=%ss", model, interval)

    while _running:
        start = time.time()
        try:
            run_cycle(model)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            logger.exception("cycle failed: %s", exc)
        # sleep the remainder of the interval, responsive to shutdown
        elapsed = time.time() - start
        remaining = max(0.0, interval - elapsed)
        slept = 0.0
        while _running and slept < remaining:
            time.sleep(min(1.0, remaining - slept))
            slept += 1.0
    logger.info("sidecar stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
