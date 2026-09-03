"""Redis market-data bus: recorder publishes ticks/contracts; web consumes them.

Channels (configurable via env):
  MD_BUS_TICK_CHANNEL      default vnpy:md:tick
  MD_BUS_CONTRACT_CHANNEL  default vnpy:md:contract
  MD_BUS_LATEST_KEY        default vnpy:md:latest   (hash vt_symbol -> tick json)
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from datetime import datetime
from typing import Any, Callable

from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Exchange, OptionType, Product
from vnpy.trader.event import EVENT_CONTRACT, EVENT_TICK
from vnpy.trader.object import ContractData, TickData


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def md_bus_enabled() -> bool:
    raw = (os.getenv("LIVE_MD_SOURCE") or "").strip().lower()
    if raw in {"redis", "bus", "md_bus"}:
        return True
    return (os.getenv("MD_BUS_ENABLE") or "").strip().lower() in {"1", "true", "yes", "on"}


def redis_url() -> str:
    return _env("REDIS_URL", "redis://redis:6379/0")


def tick_channel() -> str:
    return _env("MD_BUS_TICK_CHANNEL", "vnpy:md:tick")


def contract_channel() -> str:
    return _env("MD_BUS_CONTRACT_CHANNEL", "vnpy:md:contract")


def latest_key() -> str:
    return _env("MD_BUS_LATEST_KEY", "vnpy:md:latest")


def contracts_key() -> str:
    return _env("MD_BUS_CONTRACTS_KEY", "vnpy:md:contracts")


def _dt_to_str(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _dt_from_str(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def tick_to_dict(tick: TickData) -> dict[str, Any]:
    return {
        "gateway_name": tick.gateway_name,
        "symbol": tick.symbol,
        "exchange": tick.exchange.value if isinstance(tick.exchange, Exchange) else str(tick.exchange),
        "datetime": _dt_to_str(tick.datetime),
        "name": tick.name,
        "volume": tick.volume,
        "turnover": tick.turnover,
        "open_interest": tick.open_interest,
        "last_price": tick.last_price,
        "last_volume": tick.last_volume,
        "limit_up": tick.limit_up,
        "limit_down": tick.limit_down,
        "open_price": tick.open_price,
        "high_price": tick.high_price,
        "low_price": tick.low_price,
        "pre_close": tick.pre_close,
        "bid_price_1": tick.bid_price_1,
        "bid_price_2": tick.bid_price_2,
        "bid_price_3": tick.bid_price_3,
        "bid_price_4": tick.bid_price_4,
        "bid_price_5": tick.bid_price_5,
        "ask_price_1": tick.ask_price_1,
        "ask_price_2": tick.ask_price_2,
        "ask_price_3": tick.ask_price_3,
        "ask_price_4": tick.ask_price_4,
        "ask_price_5": tick.ask_price_5,
        "bid_volume_1": tick.bid_volume_1,
        "bid_volume_2": tick.bid_volume_2,
        "bid_volume_3": tick.bid_volume_3,
        "bid_volume_4": tick.bid_volume_4,
        "bid_volume_5": tick.bid_volume_5,
        "ask_volume_1": tick.ask_volume_1,
        "ask_volume_2": tick.ask_volume_2,
        "ask_volume_3": tick.ask_volume_3,
        "ask_volume_4": tick.ask_volume_4,
        "ask_volume_5": tick.ask_volume_5,
        "localtime": _dt_to_str(tick.localtime),
    }


def tick_from_dict(data: dict[str, Any]) -> TickData:
    exchange = data.get("exchange") or "LOCAL"
    try:
        exch = Exchange(exchange)
    except ValueError:
        exch = Exchange.LOCAL
    tick = TickData(
        gateway_name=str(data.get("gateway_name") or "REDIS"),
        symbol=str(data["symbol"]),
        exchange=exch,
        datetime=_dt_from_str(data.get("datetime")) or datetime.now(),
        name=str(data.get("name") or ""),
        volume=float(data.get("volume") or 0),
        turnover=float(data.get("turnover") or 0),
        open_interest=float(data.get("open_interest") or 0),
        last_price=float(data.get("last_price") or 0),
        last_volume=float(data.get("last_volume") or 0),
        limit_up=float(data.get("limit_up") or 0),
        limit_down=float(data.get("limit_down") or 0),
        open_price=float(data.get("open_price") or 0),
        high_price=float(data.get("high_price") or 0),
        low_price=float(data.get("low_price") or 0),
        pre_close=float(data.get("pre_close") or 0),
        bid_price_1=float(data.get("bid_price_1") or 0),
        bid_price_2=float(data.get("bid_price_2") or 0),
        bid_price_3=float(data.get("bid_price_3") or 0),
        bid_price_4=float(data.get("bid_price_4") or 0),
        bid_price_5=float(data.get("bid_price_5") or 0),
        ask_price_1=float(data.get("ask_price_1") or 0),
        ask_price_2=float(data.get("ask_price_2") or 0),
        ask_price_3=float(data.get("ask_price_3") or 0),
        ask_price_4=float(data.get("ask_price_4") or 0),
        ask_price_5=float(data.get("ask_price_5") or 0),
        bid_volume_1=float(data.get("bid_volume_1") or 0),
        bid_volume_2=float(data.get("bid_volume_2") or 0),
        bid_volume_3=float(data.get("bid_volume_3") or 0),
        bid_volume_4=float(data.get("bid_volume_4") or 0),
        bid_volume_5=float(data.get("bid_volume_5") or 0),
        ask_volume_1=float(data.get("ask_volume_1") or 0),
        ask_volume_2=float(data.get("ask_volume_2") or 0),
        ask_volume_3=float(data.get("ask_volume_3") or 0),
        ask_volume_4=float(data.get("ask_volume_4") or 0),
        ask_volume_5=float(data.get("ask_volume_5") or 0),
        localtime=_dt_from_str(data.get("localtime")),
    )
    return tick


def contract_to_dict(contract: ContractData) -> dict[str, Any]:
    return {
        "gateway_name": contract.gateway_name,
        "symbol": contract.symbol,
        "exchange": contract.exchange.value if isinstance(contract.exchange, Exchange) else str(contract.exchange),
        "name": contract.name,
        "product": contract.product.value if isinstance(contract.product, Product) else str(contract.product),
        "size": contract.size,
        "pricetick": contract.pricetick,
        "min_volume": getattr(contract, "min_volume", 1),
        "option_strike": getattr(contract, "option_strike", 0) or 0,
        "option_underlying": getattr(contract, "option_underlying", "") or "",
        "option_type": (
            contract.option_type.value
            if getattr(contract, "option_type", None) is not None
            and isinstance(contract.option_type, OptionType)
            else (str(contract.option_type) if getattr(contract, "option_type", None) else "")
        ),
        "option_expiry": _dt_to_str(getattr(contract, "option_expiry", None)),
        "option_portfolio": getattr(contract, "option_portfolio", "") or "",
        "option_index": getattr(contract, "option_index", "") or "",
    }


def contract_from_dict(data: dict[str, Any]) -> ContractData:
    try:
        exch = Exchange(data.get("exchange") or "LOCAL")
    except ValueError:
        exch = Exchange.LOCAL
    try:
        product = Product(data.get("product") or "期权")
    except ValueError:
        product = Product.OPTION
    option_type = None
    raw_ot = data.get("option_type") or ""
    if raw_ot:
        try:
            option_type = OptionType(raw_ot)
        except ValueError:
            option_type = None
    contract = ContractData(
        gateway_name=str(data.get("gateway_name") or "REDIS"),
        symbol=str(data["symbol"]),
        exchange=exch,
        name=str(data.get("name") or data["symbol"]),
        product=product,
        size=float(data.get("size") or 1),
        pricetick=float(data.get("pricetick") or 0.2),
    )
    if hasattr(contract, "min_volume"):
        contract.min_volume = float(data.get("min_volume") or 1)
    if option_type is not None:
        contract.option_type = option_type
    if data.get("option_strike"):
        contract.option_strike = float(data["option_strike"])
    if data.get("option_underlying"):
        contract.option_underlying = str(data["option_underlying"])
    if data.get("option_expiry"):
        contract.option_expiry = _dt_from_str(data.get("option_expiry"))
    if data.get("option_portfolio"):
        contract.option_portfolio = str(data["option_portfolio"])
    if data.get("option_index"):
        contract.option_index = str(data["option_index"])
    return contract


def create_redis_client():
    import redis

    return redis.Redis.from_url(redis_url(), decode_responses=True)


class MdBusPublisher:
    """Publish CTP ticks/contracts onto Redis for downstream consumers."""

    def __init__(self, event_engine: EventEngine, log: Callable[[str], None] | None = None) -> None:
        self.event_engine = event_engine
        self.log = log or (lambda msg: print(f"[MD_BUS] {msg}", flush=True))
        self._client = None
        self._pub_count = 0
        self._err_count = 0
        self._last_err = ""
        self._lock = threading.Lock()

    def start(self) -> None:
        self._client = create_redis_client()
        self._client.ping()
        self.event_engine.register(EVENT_TICK, self._on_tick)
        self.event_engine.register(EVENT_CONTRACT, self._on_contract)
        self.log(f"publisher started redis={redis_url()} tick={tick_channel()}")

    def stop(self) -> None:
        try:
            self.event_engine.unregister(EVENT_TICK, self._on_tick)
            self.event_engine.unregister(EVENT_CONTRACT, self._on_contract)
        except Exception:
            pass
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "role": "publisher",
            "redis": redis_url(),
            "tick_channel": tick_channel(),
            "pub_count": self._pub_count,
            "err_count": self._err_count,
            "last_err": self._last_err,
        }

    def _on_tick(self, event: Event) -> None:
        tick = event.data
        if not isinstance(tick, TickData):
            return
        self._publish_tick(tick)

    def _on_contract(self, event: Event) -> None:
        contract = event.data
        if not isinstance(contract, ContractData):
            return
        self._publish_contract(contract)

    def _publish_tick(self, tick: TickData) -> None:
        if self._client is None:
            return
        try:
            payload = json.dumps(tick_to_dict(tick), ensure_ascii=False, separators=(",", ":"))
            pipe = self._client.pipeline(transaction=False)
            pipe.publish(tick_channel(), payload)
            pipe.hset(latest_key(), tick.vt_symbol, payload)
            pipe.execute()
            with self._lock:
                self._pub_count += 1
        except Exception as exc:
            with self._lock:
                self._err_count += 1
                self._last_err = str(exc)

    def _publish_contract(self, contract: ContractData) -> None:
        if self._client is None:
            return
        try:
            payload = json.dumps(contract_to_dict(contract), ensure_ascii=False, separators=(",", ":"))
            pipe = self._client.pipeline(transaction=False)
            pipe.publish(contract_channel(), payload)
            pipe.hset(contracts_key(), contract.vt_symbol, payload)
            pipe.execute()
        except Exception as exc:
            with self._lock:
                self._err_count += 1
                self._last_err = str(exc)


class MdBusSubscriber:
    """Consume Redis ticks/contracts and inject into local EventEngine."""

    def __init__(self, event_engine: EventEngine, log: Callable[[str], None] | None = None) -> None:
        self.event_engine = event_engine
        self.log = log or (lambda msg: print(f"[MD_BUS] {msg}", flush=True))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client = None
        self._pubsub = None
        self._tick_count = 0
        self._contract_count = 0
        self._err_count = 0
        self._last_err = ""
        self._last_tick_vt = ""
        self._last_tick_dt = ""

    def start(self) -> None:
        self._client = create_redis_client()
        self._client.ping()
        self._warmup_latest()
        self._thread = threading.Thread(target=self._loop, name="md-bus-subscriber", daemon=True)
        self._thread.start()
        self.log(f"subscriber started redis={redis_url()} tick={tick_channel()}")

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._pubsub is not None:
                self._pubsub.close()
        except Exception:
            pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "role": "subscriber",
            "redis": redis_url(),
            "tick_channel": tick_channel(),
            "tick_count": self._tick_count,
            "contract_count": self._contract_count,
            "err_count": self._err_count,
            "last_err": self._last_err,
            "last_tick_vt": self._last_tick_vt,
            "last_tick_dt": self._last_tick_dt,
            "thread_alive": bool(self._thread and self._thread.is_alive()),
        }

    def _warmup_latest(self) -> None:
        assert self._client is not None
        try:
            mapping = self._client.hgetall(latest_key()) or {}
            for raw in mapping.values():
                self._ingest_tick_json(raw)
            if mapping:
                self.log(f"warmed {len(mapping)} latest ticks from Redis")
        except Exception as exc:
            self._err_count += 1
            self._last_err = f"warmup: {exc}"
        try:
            contracts = self._client.hgetall(contracts_key()) or {}
            for raw in contracts.values():
                self._ingest_contract_json(raw)
            if contracts:
                self.log(f"warmed {len(contracts)} contracts from Redis")
        except Exception as exc:
            self._err_count += 1
            self._last_err = f"contract_warmup: {exc}"

    def _loop(self) -> None:
        assert self._client is not None
        while not self._stop.is_set():
            try:
                self._pubsub = self._client.pubsub(ignore_subscribe_messages=True)
                self._pubsub.subscribe(tick_channel(), contract_channel())
                for message in self._pubsub.listen():
                    if self._stop.is_set():
                        break
                    if not message or message.get("type") != "message":
                        continue
                    channel = message.get("channel")
                    data = message.get("data")
                    if channel == tick_channel():
                        self._ingest_tick_json(data)
                    elif channel == contract_channel():
                        self._ingest_contract_json(data)
            except Exception as exc:
                self._err_count += 1
                self._last_err = str(exc)
                traceback.print_exc()
                self._stop.wait(1.5)

    def _ingest_tick_json(self, raw: str | bytes | None) -> None:
        if not raw:
            return
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            tick = tick_from_dict(json.loads(raw))
            self.event_engine.put(Event(EVENT_TICK, tick))
            self._tick_count += 1
            self._last_tick_vt = tick.vt_symbol
            self._last_tick_dt = _dt_to_str(tick.datetime) or ""
        except Exception as exc:
            self._err_count += 1
            self._last_err = str(exc)

    def _ingest_contract_json(self, raw: str | bytes | None) -> None:
        if not raw:
            return
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            contract = contract_from_dict(json.loads(raw))
            self.event_engine.put(Event(EVENT_CONTRACT, contract))
            self._contract_count += 1
        except Exception as exc:
            self._err_count += 1
            self._last_err = str(exc)


_publisher: MdBusPublisher | None = None
_subscriber: MdBusSubscriber | None = None


def start_md_bus_publisher(event_engine: EventEngine, log: Callable[[str], None] | None = None) -> MdBusPublisher:
    global _publisher
    if _publisher is not None:
        return _publisher
    pub = MdBusPublisher(event_engine, log=log)
    pub.start()
    _publisher = pub
    return pub


def start_md_bus_subscriber(event_engine: EventEngine, log: Callable[[str], None] | None = None) -> MdBusSubscriber:
    global _subscriber
    if _subscriber is not None:
        return _subscriber
    sub = MdBusSubscriber(event_engine, log=log)
    sub.start()
    _subscriber = sub
    return sub


def md_bus_status() -> dict[str, Any]:
    if _publisher is not None:
        return _publisher.status()
    if _subscriber is not None:
        return _subscriber.status()
    return {"enabled": False}


def stop_md_bus() -> None:
    global _publisher, _subscriber
    if _publisher is not None:
        _publisher.stop()
        _publisher = None
    if _subscriber is not None:
        _subscriber.stop()
        _subscriber = None
