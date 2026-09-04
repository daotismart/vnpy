"""Recorder: Redis Stream → QuestDB.

No CTP. Consumes durable tick stream with a consumer group and ACK only after
successful QuestDB write (at-least-once; restart resumes pending messages).
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
from vnpy.trader.object import TickData

from md_bus import (
    md_bus_status,
    start_md_stream_consumer,
    stop_md_bus,
    tick_stream,
)


HEARTBEAT_PATH = Path(_env("RECORDER_HEARTBEAT_FILE", "/tmp/recorder_heartbeat"))
_stop = threading.Event()
_write_count = 0
_write_err = 0
_last_err = ""
_last_vt = ""
_last_dt = ""
_lock = threading.Lock()


def _on_ticks(batch: list[tuple[str, TickData]]) -> None:
    """Persist batch to QuestDB. Raise on failure so consumer does not ACK."""
    global _write_count, _write_err, _last_err, _last_vt, _last_dt
    ticks = [tick for _msg_id, tick in batch]
    if not ticks:
        return
    db = get_database()
    ok = db.save_tick_data(ticks, stream=True)
    if not ok:
        raise RuntimeError("QuestDB save_tick_data returned False")
    with _lock:
        _write_count += len(ticks)
        _last_vt = ticks[-1].vt_symbol
        _last_dt = str(ticks[-1].datetime)


def _write_heartbeat() -> None:
    try:
        bus = md_bus_status()
        with _lock:
            payload = (
                f"ts={time.time():.3f}\n"
                f"write_count={_write_count}\n"
                f"write_err={_write_err}\n"
                f"last_vt={_last_vt}\n"
                f"last_dt={_last_dt}\n"
                f"stream={tick_stream()}\n"
                f"read_count={bus.get('read_count')}\n"
                f"ack_count={bus.get('ack_count')}\n"
                f"pending={bus.get('pending')}\n"
                f"bus_err={bus.get('err_count')}\n"
                f"last_err={bus.get('last_err') or _last_err}\n"
            )
        HEARTBEAT_PATH.write_text(payload, encoding="utf-8")
    except Exception as exc:
        try:
            HEARTBEAT_PATH.write_text(f"ts={time.time():.3f}\nerror={exc}\n", encoding="utf-8")
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
        f"stream={tick_stream()} group={consumer.group}",
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
