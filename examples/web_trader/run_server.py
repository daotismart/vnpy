"""Headless VeighNa Web Trader entry point."""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vnpy.trader.setting import SETTINGS


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


# Override only in this process so desktop vt_setting.json (TDengine) cannot block startup.
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

import uvicorn
from vnpy.event import EventEngine
from vnpy.event.engine import Event
from vnpy.trader.engine import MainEngine
from vnpy_ctp import CtpGateway
from vnpy_ctabacktester import CtaBacktesterApp
from vnpy_ctastrategy import CtaStrategyApp
from vnpy_datamanager import DataManagerApp
from vnpy_datarecorder import DataRecorderApp
from vnpy_optionmaster import OptionMasterApp
from vnpy_optionmaster.base import OptionData
from vnpy_scripttrader import ScriptTraderApp
from vnpy_spreadtrading import SpreadTradingApp

from ctp_session import patch_ctp_connect_modes
from md_bus import md_bus_enabled, start_md_bus_subscriber, stop_md_bus
from server import app, attach_runtime


HOST = _env("WEB_HOST", "127.0.0.1")
PORT = int(_env("WEB_PORT", "8000"))


def _patch_event_engine() -> None:
    """Keep tick dispatch alive when a single app handler raises."""
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
    """OptionMaster options may exist before set_underlying()."""
    if getattr(OptionData.calculate_theo_greeks, "_web_trader_safe", False):
        return
    original = OptionData.calculate_theo_greeks

    def calculate_theo_greeks(self) -> None:
        if not getattr(self, "underlying", None):
            return
        original(self)

    calculate_theo_greeks._web_trader_safe = True  # type: ignore[attr-defined]
    OptionData.calculate_theo_greeks = calculate_theo_greeks  # type: ignore[method-assign]


def main() -> None:
    # Default web role when Redis MD bus is used: TD for trading, MD from Redis.
    if md_bus_enabled():
        os.environ.setdefault("LIVE_CTP_SKIP_MD", "1")
        if os.getenv("LIVE_RECORD_TICKS") in (None, ""):
            os.environ["LIVE_RECORD_TICKS"] = "0"

    patch_ctp_connect_modes()
    _patch_event_engine()
    _patch_option_greeks()

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    main_engine.add_gateway(CtpGateway)
    cta_engine = main_engine.add_app(CtaStrategyApp)
    backtester_engine = main_engine.add_app(CtaBacktesterApp)
    data_engine = main_engine.add_app(DataManagerApp)
    recorder_engine = main_engine.add_app(DataRecorderApp)
    option_engine = main_engine.add_app(OptionMasterApp)
    spread_engine = main_engine.add_app(SpreadTradingApp)
    script_engine = main_engine.add_app(ScriptTraderApp)

    cta_engine.init_engine()
    backtester_engine.init_engine()
    spread_engine.start()
    script_engine.init()

    if md_bus_enabled():
        start_md_bus_subscriber(event_engine, log=lambda m: main_engine.write_log(f"[MD_BUS] {m}"))
        # Keep Redis contract hash warm from web TD so md_receiver can MD-only recover
        # after Redis restarts without needing a TD seat.
        # IMPORTANT: never PUBLISH back to the contract channel from this handler —
        # the MD bus subscriber also emits EVENT_CONTRACT, which would feedback-loop
        # and cause DataRecorder to spam CTP subscribe logs.
        try:
            import threading
            import time as _time

            from vnpy.trader.event import EVENT_CONTRACT
            from vnpy.trader.object import ContractData
            from md_bus import store_contracts_to_redis

            _pending: list = []
            _lock = threading.Lock()
            _last_flush = 0.0

            def _flush_pending(force: bool = False) -> None:
                nonlocal _last_flush
                now = _time.time()
                with _lock:
                    if not _pending:
                        return
                    if not force and now - _last_flush < 5 and len(_pending) < 100:
                        return
                    batch = list(_pending)
                    _pending.clear()
                    _last_flush = now
                try:
                    store_contracts_to_redis(batch, publish=False)
                except Exception:
                    pass

            def _seed_contract(event) -> None:
                contract = event.data
                if not isinstance(contract, ContractData):
                    return
                symbol = str(contract.symbol or "").upper()
                if not symbol.startswith(("IF", "IO", "IH", "IC", "IM")):
                    return
                with _lock:
                    _pending.append(contract)
                _flush_pending(False)

            event_engine.register(EVENT_CONTRACT, _seed_contract)
            existing = [
                c
                for c in (main_engine.get_all_contracts() or [])
                if str(getattr(c, "symbol", "") or "").upper().startswith(("IF", "IO", "IH", "IC", "IM"))
            ]
            if existing:
                result = store_contracts_to_redis(existing, publish=False)
                main_engine.write_log(f"[MD_BUS] seeded Redis contracts from web TD: {result}")
        except Exception:
            traceback.print_exc()

        # Web uses Redis MD — DataRecorder must not re-subscribe via CTP on every
        # contract event (useless + log spam when SKIP_MD=1).
        try:
            from vnpy_datarecorder.engine import RecorderEngine

            def _skip_ctp_subscribe(self, contract) -> None:  # noqa: ANN001
                return

            RecorderEngine.subscribe = _skip_ctp_subscribe  # type: ignore[method-assign]
            main_engine.write_log("[MD_BUS] DataRecorder CTP subscribe disabled (Redis MD)")
        except Exception:
            traceback.print_exc()

    attach_runtime(
        main_engine,
        event_engine,
        cta_engine,
        backtester_engine,
        data_engine,
        recorder_engine,
        option_engine,
        spread_engine,
        script_engine,
    )

    url = f"http://{HOST}:{PORT}/"
    print(f"VeighNa Web Trader started: {url}")
    print(
        "Database: QuestDB "
        f"{SETTINGS['database.host']}:{SETTINGS['database.port']} "
        f"(HTTP ILP {SETTINGS['database.http_port']})"
    )
    if md_bus_enabled():
        print(f"Market data: Redis MD bus ({_env('REDIS_URL', 'redis://redis:6379/0')})")
    print("Login with username/password from ~/.vntrader/web_trader_setting.json (default vnpy / vnpy)")

    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="info")
    finally:
        try:
            stop_md_bus()
        except Exception:
            traceback.print_exc()
        main_engine.close()


if __name__ == "__main__":
    main()
