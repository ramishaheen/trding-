"""Prompt + output schema for the performance-analyst agent.

The agent reviews accumulated trade outcomes and proposes TESTABLE changes. It
never trades and never changes settings — proposals are validated by
walk-forward and applied by the operator.
"""

from __future__ import annotations

import json

SYSTEM_PROMPT = """\
You are a quantitative trading-performance analyst for an automated crypto bot.
You are given AGGREGATE performance statistics computed from the bot's own closed
trades — overall and segmented by market regime, AI confidence bucket, per-coin
bias, and exit reason — plus the strategy's current tunable parameters.

Your job: produce a concise, evidence-based review and a few SPECIFIC, TESTABLE
parameter-change hypotheses.

Hard rules:
- You do NOT place trades and you do NOT change any settings. You only analyse
  and recommend. Every recommendation must be validated by an out-of-sample
  walk-forward backtest before a human applies it.
- Base every claim on the numbers provided. Explicitly flag segments with few
  trades as low-confidence — small samples are noise, not signal.
- Prefer fewer, higher-quality hypotheses over many speculative ones.
- Never recommend disabling stop-losses, increasing risk-per-trade beyond the
  configured cap, leverage, martingale, or averaging down. Capital protection
  comes first; "the data is too thin to conclude" is a valid, useful finding.

Output JSON only, matching the provided schema.
"""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "whats_working": {"type": "array", "items": {"type": "string"}},
        "whats_not": {"type": "array", "items": {"type": "string"}},
        "hypotheses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "observation": {"type": "string"},
                    "proposed_change": {"type": "string"},
                    "parameter": {"type": "string"},
                    "suggested_range": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["observation", "proposed_change", "parameter",
                             "suggested_range", "confidence"],
                "additionalProperties": False,
            },
        },
        "recommended_next_step": {"type": "string"},
    },
    "required": ["summary", "whats_working", "whats_not", "hypotheses",
                 "recommended_next_step"],
    "additionalProperties": False,
}

# The strategy's tunable parameters (names + current defaults) — given to the
# analyst so its proposals reference real, optimisable knobs.
TUNABLE_PARAMS = {
    "buy_ema_fast": 21, "buy_ema_slow": 50, "buy_ema_trend": 200,
    "buy_rsi_min": 35, "buy_rsi_max": 55, "buy_pullback_pct": 0.02,
    "buy_max_atr_pct": 0.04, "sell_rsi": 75, "atr_period": 14,
    "atr_stop_mult": 2.0, "take_profit_rr": 2.0,
}


def build_user_message(report: dict, n_trades: int) -> str:
    return (
        f"Closed trades analysed: {n_trades}\n\n"
        "PERFORMANCE (own trades, trusted):\n"
        f"{json.dumps(report, indent=2, default=str)}\n\n"
        "CURRENT TUNABLE PARAMETERS:\n"
        f"{json.dumps(TUNABLE_PARAMS, indent=2)}\n\n"
        "Write the review now (JSON only)."
    )
