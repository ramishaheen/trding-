"""Performance analytics over the trade journal — the 'experience' the system
learns from. Pure and dependency-free so the maths is unit-tested exactly as it
runs. Operates on a list of outcome dicts, each like:

    {"profit_ratio": 0.012, "profit_abs": 1.2, "pair": "BTC/USDT",
     "regime": "trending_up", "risk_state": "risk_on", "confidence": 0.7,
     "pair_bias": "bullish", "exit_reason": "roi", "hour": 14}

Nothing here trades or changes settings — it only summarises outcomes.
"""

from __future__ import annotations

from typing import Callable, Iterable


def _stats(pnls: list[float]) -> dict:
    """Win rate / avg win / avg loss / profit factor / expectancy / total."""
    n = len(pnls)
    if n == 0:
        return {"trades": 0, "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "profit_factor": 0.0, "expectancy": 0.0, "total": 0.0}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win else 0.0)
    expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss
    return {"trades": n, "win_rate": round(win_rate, 4), "avg_win": round(avg_win, 6),
            "avg_loss": round(avg_loss, 6),
            "profit_factor": (round(pf, 3) if pf != float("inf") else None),
            "expectancy": round(expectancy, 6), "total": round(sum(pnls), 6)}


def overall(outcomes: list[dict]) -> dict:
    return _stats([float(o.get("profit_ratio", 0.0)) for o in outcomes])


def confidence_bucket(c) -> str:
    try:
        c = float(c)
    except (TypeError, ValueError):
        return "unknown"
    if c >= 0.6:
        return "high(>=0.6)"
    if c >= 0.4:
        return "medium(0.4-0.6)"
    return "low(<0.4)"


def by_segment(outcomes: list[dict], key: Callable[[dict], str] | str) -> dict:
    """Group outcomes by a field name or key function -> per-segment stats."""
    keyfn = (lambda o: str(o.get(key, "unknown"))) if isinstance(key, str) else key
    groups: dict[str, list[float]] = {}
    for o in outcomes:
        groups.setdefault(keyfn(o), []).append(float(o.get("profit_ratio", 0.0)))
    return {seg: _stats(pnls) for seg, pnls in sorted(groups.items())}


def best_and_worst(outcomes: list[dict], field: str, min_trades: int = 5) -> dict:
    """Best/worst segment of `field` by expectancy, among segments with enough
    samples to be believable."""
    segs = by_segment(outcomes, field)
    eligible = {s: v for s, v in segs.items() if v["trades"] >= min_trades}
    if not eligible:
        return {"best": None, "worst": None, "note": f"need >= {min_trades} trades/segment"}
    ranked = sorted(eligible.items(), key=lambda kv: kv[1]["expectancy"])
    return {"worst": {"segment": ranked[0][0], **ranked[0][1]},
            "best": {"segment": ranked[-1][0], **ranked[-1][1]}}


def performance_report(outcomes: list[dict]) -> dict:
    """The structured analytics bundle the analyst agent reasons over."""
    return {
        "overall": overall(outcomes),
        "by_regime": by_segment(outcomes, "regime"),
        "by_risk_state": by_segment(outcomes, "risk_state"),
        "by_pair": by_segment(outcomes, "pair"),
        "by_pair_bias": by_segment(outcomes, "pair_bias"),
        "by_confidence": by_segment(outcomes, lambda o: confidence_bucket(o.get("confidence"))),
        "by_exit_reason": by_segment(outcomes, "exit_reason"),
        "best_worst_regime": best_and_worst(outcomes, "regime"),
    }
