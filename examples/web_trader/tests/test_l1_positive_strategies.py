"""Tests for L1 positive strategies backtest helpers."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import backtest_l1_positive_strategies as bt  # noqa: E402


def test_helpers_round_and_session() -> None:
    assert abs(bt.floor_to(1.35, 0.2) - 1.2) < 1e-9
    assert abs(bt.ceil_to(1.21, 0.2) - 1.4) < 1e-9
    assert bt.in_session(datetime(2026, 9, 8, 9, 45))
    assert not bt.in_session(datetime(2026, 9, 8, 12, 0))


def test_short_straddle_positive_on_cache() -> None:
    if not bt.CACHE_DIR.exists():
        return
    out = bt.run_short_straddle(
        bt.StraddleParams(name="unit", lots=1, entry="09:35", exit="14:30", hedge_lots=0.5)
    )
    assert out["final_pnl"] > 0
    assert len(out["daily_pnl"]) == 4
    assert all(v >= 0 for v in out["daily_pnl"].values())


def test_sell_only_mm_non_negative() -> None:
    if not bt.CACHE_DIR.exists():
        return
    out = bt.run_sell_only_mm()
    assert out["final_pnl"] >= 0
    assert out["strategy"] == "sell_only_mm"
