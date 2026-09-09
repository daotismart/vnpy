"""Recorder: Redis Stream → QuestDB (ticks + 1m bars).

No CTP. Consumes durable tick stream with a consumer group and ACK only after
successful QuestDB write (at-least-once; restart resumes pending messages).

When LIVE_RECORD_BAR=1 (default), also aggregates ticks into 1-minute bars via
BarGenerator and writes them with the same durability semantics.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vnpy.trader.setting import SETTINGS


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


os.environ.setdefault("LIVE_MD_SOURCE", "redis")
os.environ.setdefault("MD_BUS_ENABLE", "1")

SETTINGS["database.name"] = _env("DATABASE_DRIVER", "questdb")
SETTINGS["database.host"] = _env("DATABASE_HOST", "127.0.0.1")
SETTINGS["database.port"] = int(_env("DATABASE_PORT", "8812"))
SETTINGS["database.database"] = _env("DATABASE_NAME", "qdb")
SETTINGS["database.user"] = _env("DATABASE_USER", "admin")
SETTINGS["database.password"] = _env("DATABASE_PASSWORD", "quest")
SETTINGS["database.http_port"] = int(_env("DATABASE_HTTP_PORT", "9000"))
SETTINGS["log.active"] = True
SETTINGS["log.console"] = True
SETTINGS["log.file"] = True

import vnpy.trader.database as database_module

database_module.database = None

from vnpy.trader.database import get_database
from vnpy.trader.object import BarData, TickData
from vnpy.trader.utility import BarGenerator

from md_bus import (
    md_bus_status,
    start_md_stream_consumer,
    stop_md_bus,
    tick_stream,
)


HEARTBEAT_PATH = Path(_env("RECORDER_HEARTBEAT_FILE", "/tmp/recorder_heartbeat"))
RECORD_BAR = _env_flag("LIVE_RECORD_BAR", True)
_stop = threading.Event()
_write_count = 0
_bar_write_count = 0
_write_err = 0
_last_err = ""
_last_vt = ""
_last_dt = ""
_lock = threading.Lock()

# Per-symbol 1m bar generators (durable consumer path).
_bar_generators: dict[str, BarGenerator] = {}
_pending_bars: list[BarData] = []
_bar_lock = threading.Lock()


def _on_completed_bar(bar: BarData) -> None:
    with _bar_lock:
        _pending_bars.append(bar)


def _get_bar_generator(vt_symbol: str) -> BarGenerator:
    bg = _bar_generators.get(vt_symbol)
    if bg is None:
        bg = BarGenerator(_on_completed_bar)
        _bar_generators[vt_symbol] = bg
    return bg


def _flush_pending_bars() -> int:
    """Write completed 1m bars to QuestDB. Returns number written."""
    global _bar_write_count
    if not RECORD_BAR:
        return 0
    with _bar_lock:
        if not _pending_bars:
            return 0
        bars = list(_pending_bars)
        _pending_bars.clear()
    db = get_database()
    ok = db.save_bar_data(bars, stream=True)
    if not ok:
        # Re-queue so the next batch retries; caller should treat as failure.
        with _bar_lock:
            _pending_bars[0:0] = bars
        raise RuntimeError("QuestDB save_bar_data returned False")
    with _lock:
        _bar_write_count += len(bars)
    return len(bars)


def _on_ticks(batch: list[tuple[str, TickData]]) -> None:
    """Persist tick batch (+ completed bars) to QuestDB.

    Raise on failure so consumer does not ACK.
    """
    global _write_count, _write_err, _last_err, _last_vt, _last_dt
    ticks = [tick for _msg_id, tick in batch]
    if not ticks:
        return
    db = get_database()
    ok = db.save_tick_data(ticks, stream=True)
    if not ok:
        raise RuntimeError("QuestDB save_tick_data returned False")

    if RECORD_BAR:
        for tick in ticks:
            if not tick.last_price:
                continue
            _get_bar_generator(tick.vt_symbol).update_tick(tick)
        _flush_pending_bars()

    with _lock:
        _write_count += len(ticks)
        _last_vt = ticks[-1].vt_symbol
        _last_dt = str(ticks[-1].datetime)


def _write_heartbeat() -> None:
    bus = md_bus_status()
    with _lock:
        write_count = _write_count
        bar_write_count = _bar_write_count
        write_err = _write_err
        last_vt = _last_vt
        last_dt = _last_dt
        last_err = _last_err
    try:
        payload = (
            f"ts={time.time():.3f}\n"
            f"write_count={write_count}\n"
            f"bar_write_count={bar_write_count}\n"
            f"record_bar={int(RECORD_BAR)}\n"
            f"bar_symbols={len(_bar_generators)}\n"
            f"write_err={write_err}\n"
            f"last_vt={last_vt}\n"
            f"last_dt={last_dt}\n"
            f"stream={tick_stream()}\n"
            f"read_count={bus.get('read_count')}\n"
            f"ack_count={bus.get('ack_count')}\n"
            f"pending={bus.get('pending')}\n"
            f"bus_err={bus.get('err_count')}\n"
            f"last_err={bus.get('last_err') or last_err}\n"
        )
        HEARTBEAT_PATH.write_text(payload, encoding="utf-8")
    except Exception as exc:
        try:
            HEARTBEAT_PATH.write_text(f"ts={time.time():.3f}\nerror={exc}\n", encoding="utf-8")
        except Exception:
            pass
    try:
        from system_monitor import write_service_heartbeat

        write_service_heartbeat(
            "recorder",
            {
                "role": "recorder",
                "write_count": write_count,
                "bar_write_count": bar_write_count,
                "record_bar": RECORD_BAR,
                "bar_symbols": len(_bar_generators),
                "write_err": write_err,
                "last_vt": last_vt,
                "last_dt": last_dt,
                "stream": tick_stream(),
                "md_bus": bus,
                "last_err": bus.get("last_err") or last_err,
            },
        )
    except Exception:
        pass


def main() -> None:
    global _write_err, _last_err

    def _safe_on_ticks(batch: list[tuple[str, TickData]]) -> None:
        global _write_err, _last_err
        try:
            _on_ticks(batch)
        except Exception as exc:
            _write_err += 1
            _last_err = str(exc)
            raise

    consumer = start_md_stream_consumer(
        _safe_on_ticks,
        log=lambda m: print(f"[RECORDER] {m}", flush=True),
    )
    print(
        "QuestDB recorder started: "
        f"db={SETTINGS['database.host']}:{SETTINGS['database.port']} "
        f"stream={tick_stream()} group={consumer.group} "
        f"record_bar={int(RECORD_BAR)}",
        flush=True,
    )

    def _on_signal(signum: int, _frame: object) -> None:
        print(f"recorder signal {signum}, shutting down", flush=True)
        _stop.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        while not _stop.wait(5.0):
            _write_heartbeat()
            # Reconnect consumer thread if it died.
            if consumer._thread is not None and not consumer._thread.is_alive() and not _stop.is_set():
                print("[RECORDER] consumer thread dead — restarting", flush=True)
                try:
                    consumer.stop()
                except Exception:
                    traceback.print_exc()
                consumer = start_md_stream_consumer(
                    _safe_on_ticks,
                    log=lambda m: print(f"[RECORDER] {m}", flush=True),
                )
    finally:
        _stop.set()
        # Best-effort flush of any bars completed in the last batch window.
        try:
            if RECORD_BAR:
                _flush_pending_bars()
        except Exception:
            traceback.print_exc()
        try:
            stop_md_bus()
        except Exception:
            traceback.print_exc()
        try:
            HEARTBEAT_PATH.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
