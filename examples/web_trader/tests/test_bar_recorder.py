"""Unit checks for standalone recorder 1m bar aggregation."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
RECORDER = ROOT / "run_recorder.py"
COMPOSE = ROOT / "docker-compose.yml"
RUN_SERVER = ROOT / "run_server.py"


def test_sources() -> None:
    recorder = RECORDER.read_text(encoding="utf-8")
    compose = COMPOSE.read_text(encoding="utf-8")
    run_server = RUN_SERVER.read_text(encoding="utf-8")

    assert "BarGenerator" in recorder
    assert "save_bar_data" in recorder
    assert "LIVE_RECORD_BAR" in recorder
    assert "bar_write_count" in recorder
    assert "LIVE_RECORD_BAR: ${LIVE_RECORD_BAR:-1}" in compose
    # web must not own bar writes in Redis-MD mode
    assert 'LIVE_RECORD_BAR: "0"' in compose
    assert "DataRecorder bar recordings cleared" in run_server


def test_bar_generator_minute_roll() -> None:
    """BarGenerator emits one completed bar when the minute changes."""
    from vnpy.trader.constant import Exchange
    from vnpy.trader.object import TickData
    from vnpy.trader.utility import BarGenerator

    tz = ZoneInfo("Asia/Shanghai")
    completed: list = []

    def on_bar(bar) -> None:
        completed.append(bar)

    bg = BarGenerator(on_bar)
    base = datetime(2026, 9, 8, 9, 30, 5, tzinfo=tz)

    def make_tick(second: int, price: float, volume: float) -> TickData:
        return TickData(
            symbol="IF2609",
            exchange=Exchange.CFFEX,
            datetime=base + timedelta(seconds=second),
            name="IF2609",
            last_price=price,
            volume=volume,
            turnover=volume * price * 300,
            open_interest=100000,
            gateway_name="TEST",
        )

    bg.update_tick(make_tick(0, 4550.0, 100))
    bg.update_tick(make_tick(10, 4551.0, 110))
    bg.update_tick(make_tick(20, 4549.0, 125))
    assert not completed

    # next minute → previous bar completes
    bg.update_tick(
        TickData(
            symbol="IF2609",
            exchange=Exchange.CFFEX,
            datetime=base + timedelta(minutes=1, seconds=1),
            name="IF2609",
            last_price=4552.0,
            volume=140,
            turnover=140 * 4552.0 * 300,
            open_interest=100010,
            gateway_name="TEST",
        )
    )
    assert len(completed) == 1
    bar = completed[0]
    assert bar.open_price == 4550.0
    assert bar.high_price == 4551.0
    assert bar.low_price == 4549.0
    assert bar.close_price == 4549.0
    assert bar.volume == 25  # 125 - 100
    assert bar.datetime.minute == 30
    assert bar.datetime.second == 0


def main() -> int:
    test_sources()
    try:
        test_bar_generator_minute_roll()
    except ModuleNotFoundError as exc:
        print(f"skip BarGenerator runtime check: {exc}")
    print("test_bar_recorder: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
