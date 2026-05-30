"""Tests for the backtest summary parser (version-defensive)."""

from backtest_report import extract_summary

SAMPLE = {
    "strategy": {
        "MyStrategy": {
            "total_trades": 120,
            "profit_total": 0.073,          # 7.3%
            "profit_total_abs": 73.2,
            "profit_factor": 1.42,
            "winrate": 0.58,
            "max_drawdown_account": 0.061,  # 6.1%
            "timeframe": "5m",
            "backtest_start": "2026-01-01 00:00:00",
            "backtest_end": "2026-05-30 00:00:00",
        }
    },
    "strategy_comparison": [{"key": "MyStrategy", "profit_total": 0.073}],
}


def test_extract_core_metrics():
    s = extract_summary(SAMPLE, "MyStrategy")
    assert s["strategy"] == "MyStrategy"
    assert s["profit_total_pct"] == 7.3
    assert s["profit_abs"] == 73.2
    assert s["profit_factor"] == 1.42
    assert s["winrate_pct"] == 58.0
    assert s["max_drawdown_pct"] == 6.1
    assert s["trades"] == 120
    assert s["timeframe"] == "5m"
    assert "2026-01-01" in s["range"]


def test_winrate_derived_from_wins_when_absent():
    data = {"strategy": {"S": {"total_trades": 50, "wins": 20, "profit_total": -0.02}}}
    s = extract_summary(data, "S")
    assert s["winrate_pct"] == 40.0
    assert s["profit_total_pct"] == -2.0


def test_older_key_max_drawdown_fallback():
    data = {"strategy": {"S": {"total_trades": 10, "max_drawdown": 0.09}}}
    assert extract_summary(data, "S")["max_drawdown_pct"] == 9.0


def test_picks_first_strategy_when_name_missing():
    s = extract_summary(SAMPLE, "Nonexistent")
    assert s["strategy"] == "MyStrategy"


def test_empty_is_safe():
    s = extract_summary({}, "MyStrategy")
    assert s["trades"] == 0 and s["profit_total_pct"] is None
