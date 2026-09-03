"""Standalone IF/IO tick+bar recorder process (no Web UI / no live strategies).

Runs CTP + OptionMaster + DataRecorder in an isolated process so Web API load,
script strategies, or a wedged uvicorn worker cannot stop QuestDB writes.
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


# Force recorder-only behaviour before importing server helpers.
os.environ["LIVE_IRON_CONDOR"] = "0"
os.environ.setdefault("LIVE_RECORD_TICKS", "1")
os.environ.setdefault("LIVE_RECORD_BAR", "1")
os.environ.setdefault("LIVE_RECORD_MAX_CHAINS", "0")
os.environ.setdefault("LIVE_RECORD_FILTER_WINDOW", "3600")
os.environ.setdefault("LIVE_MD_MAX_LAG_SEC", "180")
os.environ["STANDALONE_RECORDER"] = "1"

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

from vnpy.event import EventEngine
from vnpy.event.engine import Event
from vnpy.trader.engine import MainEngine
from vnpy_ctp import CtpGateway
from vnpy_datarecorder import DataRecorderApp
from vnpy_optionmaster import OptionMasterApp
from vnpy_optionmaster.base import OptionData

from server import attach_runtime, recorder_status


HEARTBEAT_PATH = Path(_env("RECORDER_HEARTBEAT_FILE", "/tmp/recorder_heartbeat"))
_stop = threading.Event()


def _patch_event_engine() -> None:
    if getattr(EventEngine._process, "_web_trader_safe", False):
        return

    def _process(self, event: Event) -> None:
        if event.type in self._handlers:
            for handler in list(self._handlers[event.type]):
                try:
                    handler(event)
                except Exception:
                    traceback.print_exc()
        if self._general_handlers:
            for handler in list(self._general_handlers):
                try:
                    handler(event)
                except Exception:
                    traceback.print_exc()

    _process._web_trader_safe = True  # type: ignore[attr-defined]
    EventEngine._process = _process  # type: ignore[method-assign]


def _patch_option_greeks() -> None:
    if getattr(OptionData.calculate_theo_greeks, "_web_trader_safe", False):
        return
    original = OptionData.calculate_theo_greeks

    def calculate_theo_greeks(self) -> None:
        if not getattr(self, "underlying", None):
            return
        original(self)

    calculate_theo_greeks._web_trader_safe = True  # type: ignore[attr-defined]
    OptionData.calculate_theo_greeks = calculate_theo_greeks  # type: ignore[method-assign]


def _write_heartbeat() -> None:
    try:
        status = recorder_status()
        payload = (
            f"ts={time.time():.3f}\n"
            f"active={status.get('active')}\n"
            f"pending={status.get('pending')}\n"
            f"ticks={len(status.get('tick') or [])}\n"
            f"md_lag_sec={status.get('md_lag_sec')}\n"
            f"newest={status.get('newest_tick')}\n"
        )
        HEARTBEAT_PATH.write_text(payload, encoding="utf-8")
    except Exception as exc:
        try:
            HEARTBEAT_PATH.write_text(f"ts={time.time():.3f}\nerror={exc}\n", encoding="utf-8")
        except Exception:
            pass


def _heartbeat_loop() -> None:
    while not _stop.wait(5.0):
        _write_heartbeat()


def main() -> None:
    _patch_event_engine()
    _patch_option_greeks()

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    main_engine.add_gateway(CtpGateway)
    recorder_engine = main_engine.add_app(DataRecorderApp)
    option_engine = main_engine.add_app(OptionMasterApp)

    # attach_runtime expects the full web stack; pass None for unused engines.
    attach_runtime(
        main_engine,
        event_engine,
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        recorder_engine,
        option_engine,
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )

    print(
        "Standalone recorder started: "
        f"db={SETTINGS['database.host']}:{SETTINGS['database.port']} "
        f"portfolios={os.getenv('LIVE_PORTFOLIOS', 'IO.CFFEX')} "
        f"filter_window={os.getenv('LIVE_RECORD_FILTER_WINDOW', '3600')}s",
        flush=True,
    )

    hb = threading.Thread(target=_heartbeat_loop, name="recorder-heartbeat", daemon=True)
    hb.start()
    _write_heartbeat()

    def _handle_stop(signum: int, _frame: object) -> None:
        print(f"Standalone recorder received signal {signum}, shutting down", flush=True)
        _stop.set()

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    try:
        while not _stop.wait(1.0):
            pass
    finally:
        _stop.set()
        try:
            main_engine.close()
        except Exception:
            traceback.print_exc()
        try:
            HEARTBEAT_PATH.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
