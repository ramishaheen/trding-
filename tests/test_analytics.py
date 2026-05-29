"""Tests for the learning-loop analytics (the 'experience' summariser)."""

import pytest

from analytics import (
    overall, by_segment, confidence_bucket, best_and_worst, performance_report,
)


def _o(pr, **kw):
    d = {"profit_ratio": pr, "regime": "trending_up", "risk_state": "risk_on",
         "pair": "BTC/USDT", "pair_bias": "bullish", "exit_reason": "roi", "confidence": 0.7}
    d.update(kw)
    return d


def test_overall_winrate_and_expectancy():
    outs = [_o(0.02), _o(0.02), _o(-0.01), _o(-0.01)]
    s = overall(outs)
    assert s["trades"] == 4
    assert s["win_rate"] == 0.5
    assert s["avg_win"] == pytest.approx(0.02)
    assert s["avg_loss"] == pytest.approx(0.01)
    # expectancy = .5*.02 - .5*.01 = .005
    assert s["expectancy"] == pytest.approx(0.005)
    assert s["total"] == pytest.approx(0.02)


def test_profit_factor():
    s = overall([_o(0.03), _o(-0.01)])  # gross win .03 / gross loss .01 = 3.0
    assert s["profit_factor"] == pytest.approx(3.0)


def test_empty():
    s = overall([])
    assert s["trades"] == 0 and s["expectancy"] == 0.0


def test_confidence_bucket():
    assert confidence_bucket(0.8) == "high(>=0.6)"
    assert confidence_bucket(0.5) == "medium(0.4-0.6)"
    assert confidence_bucket(0.1) == "low(<0.4)"
    assert confidence_bucket(None) == "unknown"


def test_by_segment_groups_by_regime():
    outs = [_o(0.02, regime="trending_up"), _o(-0.03, regime="ranging"),
            _o(0.01, regime="trending_up")]
    seg = by_segment(outs, "regime")
    assert seg["trending_up"]["trades"] == 2
    assert seg["ranging"]["trades"] == 1
    assert seg["ranging"]["total"] == pytest.approx(-0.03)


def test_by_segment_with_keyfn():
    outs = [_o(0.01, confidence=0.9), _o(-0.01, confidence=0.2)]
    seg = by_segment(outs, lambda o: confidence_bucket(o["confidence"]))
    assert "high(>=0.6)" in seg and "low(<0.4)" in seg


def test_best_and_worst_needs_min_trades():
    outs = [_o(0.02, regime="trending_up")] * 6 + [_o(-0.05, regime="ranging")] * 6
    bw = best_and_worst(outs, "regime", min_trades=5)
    assert bw["best"]["segment"] == "trending_up"
    assert bw["worst"]["segment"] == "ranging"
    # too few per segment -> note, no pick
    assert best_and_worst(outs[:3], "regime", min_trades=5)["best"] is None


def test_performance_report_shape():
    outs = [_o(0.02), _o(-0.01)]
    rep = performance_report(outs)
    for k in ["overall", "by_regime", "by_pair_bias", "by_confidence", "by_exit_reason"]:
        assert k in rep
