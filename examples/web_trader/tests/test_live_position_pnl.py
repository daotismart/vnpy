"""Unit tests for live account/position floating PnL helpers."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"


def _load_server_helpers():
    """Load only the pure helpers without importing the full FastAPI app stack."""
    # Minimal stubs so importing server.py constants path is unnecessary —
    # instead exec the helper source block via a lightweight module.
    mod = types.ModuleType("live_pos_pnl_helpers")
    # Import Direction from vnpy if available; else stub.
    try:
        from vnpy.trader.constant import Direction
    except Exception:
        class Direction:  # type: ignore
            LONG = "多"
            SHORT = "空"

            def __init__(self, value):
                self.value = value

    source = SERVER.read_text(encoding="utf-8")
    start = source.index("def direction_pnl_sign")
    end = source.index("\ndef account_net_pos")
    chunk = source[start:end]
    # Provide dependencies used by the helpers.
    ns: dict = {
        "Any": object,
        "Direction": Direction,
        "MainEngine": object,
        "main_engine": None,
        "time": __import__("time"),
    }
    # Stub tick_last_price used by resolve_mark_price
    def tick_last_price(tick):
        if not tick:
            return 0.0
        last = float(getattr(tick, "last_price", 0) or 0)
        if last:
            return last
        bid = float(getattr(tick, "bid_price_1", 0) or 0)
        ask = float(getattr(tick, "ask_price_1", 0) or 0)
        if bid and ask:
            return (bid + ask) / 2
        return bid or ask

    ns["tick_last_price"] = tick_last_price
    exec(chunk, ns)
    return ns


def test_long_short_pnl_sign_and_formula():
    h = _load_server_helpers()
    assert h["direction_pnl_sign"]("多") == 1.0
    assert h["direction_pnl_sign"]("空") == -1.0
    long_pnl = h["compute_position_floating_pnl"](
        direction="多",
        volume=2,
        avg_price=46.25,
        mark_price=50.0,
        size=20,
    )
    # (50-46.25)*2*20 = 150
    assert long_pnl == 150.0
    short_pnl = h["compute_position_floating_pnl"](
        direction="空",
        volume=1,
        avg_price=26.5,
        mark_price=20.0,
        size=20,
    )
    # (20-26.5)*1*20*(-1) = 130
    assert short_pnl == 130.0


def test_fallback_when_mark_missing():
    h = _load_server_helpers()
    assert (
        h["compute_position_floating_pnl"](
            direction="多",
            volume=1,
            avg_price=10,
            mark_price=0,
            size=20,
            fallback_pnl=12.34,
        )
        == 12.34
    )


def test_app_js_uses_account_pnl_not_frozen():
    app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "pnl: item.pnl ?? \"\"" in app_js
    assert "pnl: item.frozen ?? \"\"" not in app_js
    assert "function fmtPnl" in app_js


if __name__ == "__main__":
    test_long_short_pnl_sign_and_formula()
    test_fallback_when_mark_missing()
    test_app_js_uses_account_pnl_not_frozen()
    print("ok: live position pnl")
