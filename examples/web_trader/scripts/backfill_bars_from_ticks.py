"""One-shot: rebuild 1m bars in QuestDB from existing tick data.

Usage (inside recorder/web container or host with DB access):
  python scripts/backfill_bars_from_ticks.py
  python scripts/backfill_bars_from_ticks.py --symbol IF2609 --exchange CFFEX
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vnpy.trader.database import get_database
from vnpy.trader.setting import SETTINGS
from vnpy.trader.utility import BarGenerator

import vnpy.trader.database as database_module


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def configure_db() -> None:
    SETTINGS["database.name"] = _env("DATABASE_DRIVER", "questdb")
    SETTINGS["database.host"] = _env("DATABASE_HOST", "127.0.0.1")
    SETTINGS["database.port"] = int(_env("DATABASE_PORT", "8812"))
    SETTINGS["database.database"] = _env("DATABASE_NAME", "qdb")
    SETTINGS["database.user"] = _env("DATABASE_USER", "admin")
    SETTINGS["database.password"] = _env("DATABASE_PASSWORD", "quest")
    SETTINGS["database.http_port"] = int(_env("DATABASE_HTTP_PORT", "9000"))
    database_module.database = None


def backfill(
    symbol: str | None = None,
    exchange: str | None = None,
    batch_flush: int = 200,
) -> dict[str, int]:
    configure_db()
    db = get_database()
    overviews = db.get_tick_overview()
    written = 0
    symbols = 0
    errors = 0
    for ov in overviews:
        if symbol and ov.symbol != symbol:
            continue
        if exchange and ov.exchange.value != exchange:
            continue
        symbols += 1
        start = ov.start
        end = ov.end
        if not start or not end:
            continue
        try:
            # Fresh DB handle per symbol — QuestDB can drop long sessions.
            database_module.database = None
            db = get_database()
            day = start.replace(hour=0, minute=0, second=0, microsecond=0)
            end_day = end + timedelta(days=1)
            completed: list = []

            def on_bar(bar) -> None:
                completed.append(bar)

            bg = BarGenerator(on_bar)
            while day < end_day:
                chunk_end = day + timedelta(days=1)
                ticks = db.load_tick_data(ov.symbol, ov.exchange, day, min(chunk_end, end_day))
                for tick in ticks:
                    if tick.last_price:
                        bg.update_tick(tick)
                if completed and len(completed) >= batch_flush:
                    db.save_bar_data(completed, stream=True)
                    written += len(completed)
                    completed.clear()
                day = chunk_end
            if bg.bar is not None:
                bar = bg.bar
                bar.datetime = bar.datetime.replace(second=0, microsecond=0)
                completed.append(bar)
                bg.bar = None
            if completed:
                db.save_bar_data(completed, stream=True)
                written += len(completed)
                completed.clear()
            print(f"backfilled {ov.symbol}.{ov.exchange.value}: ticks_end={end}", flush=True)
        except Exception as exc:
            errors += 1
            print(f"ERROR {ov.symbol}.{ov.exchange.value}: {exc}", flush=True)
            database_module.database = None
            try:
                db = get_database()
            except Exception:
                pass
    return {"symbols": symbols, "bars_written": written, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="")
    parser.add_argument("--exchange", default="")
    args = parser.parse_args()
    result = backfill(
        symbol=args.symbol or None,
        exchange=args.exchange or None,
    )
    print(result, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
