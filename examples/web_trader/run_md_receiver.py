"""MD receiver: CTP market data → Redis (Stream + pub/sub).

Keep this process minimal and restart-safe:
  - Connect CTP, subscribe IF/IO universe
  - On every tick: XADD durable stream + PUBLISH realtime + HSET latest
  - Soft/hard reconnect on MD lag or disconnect
  - Optionally release TD so web can trade on the same investor id

No QuestDB writes here — that is the recorder process.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
import traceback
from datetime import datetime
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


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


os.environ.setdefault("LIVE_MD_SOURCE", "redis")
os.environ.setdefault("MD_BUS_ENABLE", "1")
os.environ.setdefault("LIVE_MD_MAX_LAG_SEC", "90")
os.environ.setdefault("LIVE_RECORD_MAX_CHAINS", "2")
os.environ.setdefault("LIVE_MD_RELEASE_TD", "1")
os.environ.setdefault("LIVE_CTP_SKIP_MD", "0")
os.environ.setdefault("LIVE_CTP_SKIP_TD", "0")

# Logging only — MD receiver does not touch QuestDB.
SETTINGS["log.active"] = True
SETTINGS["log.console"] = True
SETTINGS["log.file"] = True

from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Exchange, Product
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_CONTRACT, EVENT_TICK
from vnpy.trader.object import ContractData, SubscribeRequest, TickData
from vnpy.trader.utility import load_json
from vnpy_ctp import CtpGateway

from ctp_session import patch_ctp_connect_modes, release_ctp_td
from md_bus import md_bus_status, start_md_bus_publisher, stop_md_bus, tick_stream


HEARTBEAT_PATH = Path(_env("MD_RECEIVER_HEARTBEAT_FILE", "/tmp/md_receiver_heartbeat"))
CONNECT_FILE = _env("CTP_CONNECT_FILE", "") or "connect_ctp.json"
GATEWAY = _env("LIVE_GATEWAY", "CTP")
_stop = threading.Event()


def _cffex_session_open(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hhmm = now.hour * 100 + now.minute
    return (900 <= hhmm <= 1135) or (1255 <= hhmm <= 1515)


def _load_ctp_setting() -> dict:
    from vnpy.trader.utility import get_file_path

    # Prefer dedicated MD connect file when present.
    for name in (
        CONNECT_FILE,
        "connect_ctp_md.json",
        "connect_ctp_recorder.json",
        "connect_ctp.json",
    ):
        if not name:
            continue
        path = get_file_path(name)
        if path.exists():
            data = load_json(name)
            if isinstance(data, dict) and data:
                return data
    return {}


def _month_key(symbol: str) -> str:
    # IF2609 / IO2609-C-4600 → 2609
    import re

    match = re.search(r"(IF|IO|IH|IC|IM)(\d{4})", symbol.upper())
    return match.group(2) if match else "9999"


class MdReceiver:
    """Thin CTP MD → Redis bridge with reconnect."""

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        self.main_engine = main_engine
        self.event_engine = event_engine
        self.max_chains = _env_int("LIVE_RECORD_MAX_CHAINS", 2)
        self.md_max_lag = max(60, _env_int("LIVE_MD_MAX_LAG_SEC", 90))
        self.prefixes = tuple(
            p.strip().upper()
            for p in _env("LIVE_MD_PREFIXES", "IF,IO").split(",")
            if p.strip()
        )
        self.subscribed: set[str] = set()
        self.contracts: dict[str, ContractData] = {}
        self.ctp_ok = False
        self.connecting = False
        self.td_released = False
        self.next_connect = 0.0
        self.connect_failures = 0
        self.last_md_check = 0.0
        self.last_md_reconnect = 0.0
        self.last_tick_dt: datetime | None = None
        self.last_tick_vt = ""
        self.tick_count = 0
        self.last_log = ""
        self.thread = threading.Thread(target=self._loop, name="md-receiver", daemon=True)
        event_engine.register(EVENT_CONTRACT, self._on_contract)
        event_engine.register(EVENT_TICK, self._on_tick)

    def start(self) -> None:
        if not self.thread.is_alive():
            self.thread.start()
        self.log(
            f"MD receiver started prefixes={','.join(self.prefixes)} "
            f"max_chains={self.max_chains} stream={tick_stream()}"
        )

    def log(self, msg: str) -> None:
        if msg == self.last_log:
            return
        self.last_log = msg
        self.main_engine.write_log(f"[MD_RX] {msg}")

    def status(self) -> dict:
        lag = None
        if self.last_tick_dt is not None:
            now = datetime.now(self.last_tick_dt.tzinfo) if self.last_tick_dt.tzinfo else datetime.now()
            lag = round((now - self.last_tick_dt).total_seconds(), 1)
        return {
            "ctp_ok": self.ctp_ok,
            "connecting": self.connecting,
            "td_released": self.td_released,
            "subscribed": len(self.subscribed),
            "contracts": len(self.contracts),
            "tick_count": self.tick_count,
            "md_lag_sec": lag,
            "last_tick_vt": self.last_tick_vt,
            "last_tick_dt": str(self.last_tick_dt) if self.last_tick_dt else "",
            "session_open": _cffex_session_open(),
        }

    def _on_contract(self, event: Event) -> None:
        contract = event.data
        if not isinstance(contract, ContractData):
            return
        symbol = (contract.symbol or "").upper()
        if not symbol.startswith(self.prefixes):
            return
        self.contracts[contract.vt_symbol] = contract
        self._maybe_subscribe(contract)

    def _allowed_months(self) -> set[str] | None:
        if self.max_chains is None or self.max_chains <= 0:
            return None
        months = sorted({_month_key(c.symbol) for c in self.contracts.values() if _month_key(c.symbol) != "9999"})
        return set(months[: self.max_chains])

    def _maybe_subscribe(self, contract: ContractData) -> None:
        if contract.vt_symbol in self.subscribed:
            return
        allowed = self._allowed_months()
        if allowed is not None and _month_key(contract.symbol) not in allowed:
            return
        if getattr(contract.exchange, "value", "") == "LOCAL":
            return
        req = SubscribeRequest(symbol=contract.symbol, exchange=contract.exchange)
        self.main_engine.subscribe(req, GATEWAY)
        self.subscribed.add(contract.vt_symbol)

    def _resubscribe_all(self) -> None:
        self.subscribed.clear()
        for contract in list(self.contracts.values()):
            self._maybe_subscribe(contract)
        self.log(f"subscribed {len(self.subscribed)} contracts")

    def _on_tick(self, event: Event) -> None:
        tick = event.data
        if not isinstance(tick, TickData):
            return
        symbol = (tick.symbol or "").upper()
        if not symbol.startswith(self.prefixes):
            return
        self.tick_count += 1
        self.last_tick_vt = tick.vt_symbol
        self.last_tick_dt = tick.datetime

    def _loop(self) -> None:
        _stop.wait(2.0)
        while not _stop.is_set():
            try:
                self._tick()
            except Exception:
                self.log("loop error\n" + traceback.format_exc())
            _stop.wait(5.0)

    def _tick(self) -> None:
        if not self._ensure_ctp():
            return
        self._ensure_md_fresh()
        if self.contracts and not self.subscribed:
            self._resubscribe_all()
        self._maybe_release_td()

    def _ensure_ctp(self) -> bool:
        if self.td_released or _env_flag("LIVE_CTP_SKIP_TD"):
            # MD-only mode after TD release.
            if self.last_tick_dt is not None or self.contracts:
                self.ctp_ok = True
                self.connecting = False
                return True
        accounts = self.main_engine.get_all_accounts() or []
        if accounts:
            if not self.ctp_ok:
                self.ctp_ok = True
                self.connecting = False
                self.connect_failures = 0
                self.log("CTP ready")
                self._resubscribe_all()
            return True
        # Also treat as ready when MD is flowing even without account snapshot.
        if self.last_tick_dt is not None and self.ctp_ok:
            return True
        now = time.time()
        if now < self.next_connect:
            return False
        setting = _load_ctp_setting()
        if not setting:
            self.log(f"missing {CONNECT_FILE} / connect_ctp.json")
            self.next_connect = now + 60
            return False
        if not _cffex_session_open():
            self.log("outside CFFEX session — slow reconnect")
        else:
            self.log(f"connecting {GATEWAY}")
        self.main_engine.connect(setting, GATEWAY)
        self.connecting = True
        self.ctp_ok = False
        self.connect_failures += 1
        backoff = 120 if not _cffex_session_open() else min(300, 30 * (2 ** min(self.connect_failures, 3)))
        self.next_connect = now + backoff
        return False

    def _ensure_md_fresh(self) -> None:
        if not _cffex_session_open():
            return
        now = time.time()
        if now - self.last_md_check < 20:
            return
        self.last_md_check = now
        if self.last_tick_dt is None:
            return
        wall = datetime.now(self.last_tick_dt.tzinfo) if self.last_tick_dt.tzinfo else datetime.now()
        lag = (wall - self.last_tick_dt).total_seconds()
        if lag <= self.md_max_lag:
            return
        if now - self.last_md_reconnect < 45:
            return
        self.last_md_reconnect = now
        setting = _load_ctp_setting()
        if not setting:
            return
        self.log(f"MD lag {int(lag)}s > {self.md_max_lag}s — close+reconnect")
        gateway = self.main_engine.gateways.get(GATEWAY)
        if gateway is not None:
            try:
                gateway.close()
            except Exception:
                traceback.print_exc()
        # After reconnect we may need TD briefly for contracts again.
        os.environ["LIVE_CTP_SKIP_TD"] = "0"
        self.td_released = False
        self.subscribed.clear()
        self.ctp_ok = False
        self.connecting = True
        self.main_engine.connect(setting, GATEWAY)
        self.connect_failures += 1
        self.next_connect = now + 30

    def _maybe_release_td(self) -> None:
        if self.td_released or not _env_flag("LIVE_MD_RELEASE_TD", True):
            return
        if not self.subscribed:
            return
        if self.tick_count < 10 and _cffex_session_open():
            # Wait until ticks are flowing in-session before yielding TD.
            return
        if not _cffex_session_open() and not self.subscribed:
            return
        gateway = self.main_engine.gateways.get(GATEWAY)
        if gateway is None:
            return
        if release_ctp_td(gateway):
            self.td_released = True
            os.environ["LIVE_CTP_SKIP_TD"] = "1"
            self.log("released CTP TD seat (MD kept) — web may login trading front")


def _write_heartbeat(receiver: MdReceiver) -> None:
    try:
        st = receiver.status()
        bus = md_bus_status()
        payload = (
            f"ts={time.time():.3f}\n"
            f"ctp_ok={st.get('ctp_ok')}\n"
            f"subscribed={st.get('subscribed')}\n"
            f"tick_count={st.get('tick_count')}\n"
            f"md_lag_sec={st.get('md_lag_sec')}\n"
            f"last_tick={st.get('last_tick_vt')} {st.get('last_tick_dt')}\n"
            f"td_released={st.get('td_released')}\n"
            f"bus_pub={bus.get('pub_count')}\n"
            f"bus_err={bus.get('err_count')}\n"
        )
        HEARTBEAT_PATH.write_text(payload, encoding="utf-8")
    except Exception as exc:
        try:
            HEARTBEAT_PATH.write_text(f"ts={time.time():.3f}\nerror={exc}\n", encoding="utf-8")
        except Exception:
            pass


def main() -> None:
    patch_ctp_connect_modes()

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    main_engine.add_gateway(CtpGateway)

    # Patch event dispatch so one bad handler cannot kill MD fanout.
    if not getattr(EventEngine._process, "_web_trader_safe", False):
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

    start_md_bus_publisher(event_engine, log=lambda m: main_engine.write_log(f"[MD_BUS] {m}"))
    receiver = MdReceiver(main_engine, event_engine)
    receiver.start()

    print(
        f"MD receiver started redis={_env('REDIS_URL', 'redis://redis:6379/0')} "
        f"stream={tick_stream()} gateway={GATEWAY}",
        flush=True,
    )

    def _on_signal(signum: int, _frame: object) -> None:
        print(f"MD receiver signal {signum}, shutting down", flush=True)
        _stop.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        while not _stop.wait(5.0):
            _write_heartbeat(receiver)
    finally:
        _stop.set()
        try:
            stop_md_bus()
        except Exception:
            traceback.print_exc()
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
