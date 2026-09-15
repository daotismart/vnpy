"""Unit tests for L1 option MM tick backtest helpers."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import backtest_l1_option_mm as bt  # noqa: E402


def test_as_quotes_inventory_skew() -> None:
    mid = 50.0
    r0, h0 = bt.as_quotes(mid, 0.0, 0.08, 1.4, 0.22, 0.15, 0.02)
    r_long, _ = bt.as_quotes(mid, 1.0, 0.08, 1.4, 0.22, 0.15, 0.02)
    r_short, _ = bt.as_quotes(mid, -1.0, 0.08, 1.4, 0.22, 0.15, 0.02)
    assert h0 > 0
    assert r_long < r0 < r_short


def test_build_quote_never_crosses_book() -> None:
    params = bt.Params(name="t")
    quote = bt.build_quote(params, theo=50.0, market_mid=50.0, bid_mkt=49.0, ask_mkt=51.0, pos=0, unit_delta=0.5, vega=10.0, rel_gamma=1.0)
    assert quote.bid > 0
    assert quote.ask > quote.bid
    assert quote.bid <= 51.0 - bt.PRICETICK
    assert quote.ask >= 49.0 + bt.PRICETICK


def test_try_fill_touch() -> None:
    params = bt.Params(name="t", fill_mode="touch", fill_at="ours")
    quote = bt.Quote(bid=50.0, ask=52.0, allow_bid=True, allow_ask=True, mid=51.0)
    book = bt.Book(bid=49.0, ask=50.0, bid_vol=2, ask_vol=2)
    side, px = bt.try_fill(quote, book, params)
    assert side == 1
    assert px == 50.0


def test_run_one_synthetic() -> None:
    base = datetime(2026, 9, 11, 9, 30, 0)
    under = []
    calls = []
    puts = []
    spot = 4500.0
    for i in range(120):
        dt = base.replace(second=i % 60, minute=30 + i // 60)
        s = spot + (i % 10 - 5) * 0.2
        under.append(
            {
                "datetime": dt.isoformat(sep=" "),
                "last_price": s,
                "volume": i,
                "bid_price_1": s - 0.2,
                "ask_price_1": s + 0.2,
                "bid_volume_1": 2,
                "ask_volume_1": 2,
            }
        )
        mid = 40 + (i % 6)
        spread = 0.2 if i % 5 == 0 else 1.0
        row = {
            "datetime": dt.isoformat(sep=" "),
            "last_price": mid,
            "volume": i,
            "bid_price_1": mid - spread / 2,
            "ask_price_1": mid + spread / 2,
            "bid_volume_1": 5,
            "ask_volume_1": 5,
        }
        calls.append(row)
        puts.append(dict(row))

    legs = [("IO2609-C-4500", 1, 4500.0), ("IO2609-P-4500", -1, 4500.0)]
    out = bt.run_one(under, {"IO2609-C-4500": calls, "IO2609-P-4500": puts}, legs, bt.PRESETS[0], date(2026, 9, 18))
    assert "final_pnl" in out
    assert out["fills"] >= 0
    assert out["quote_updates"] > 0
