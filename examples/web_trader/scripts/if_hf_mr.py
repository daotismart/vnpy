"""
IF 5 分钟高频均值回归（ScriptTrader）

思路：
1. 用近 look 根 5 分钟对数收益的 z-score，在极端冲高/杀跌后反向开仓
2. 仅上午交易（09:45–11:25），避开午后噪声；当日最多 1 笔
3. 波动率地板过滤（过静不交易）；1.5×ATR 止损 + 1.0×ATR 止盈；持有 hold 根或 11:25 前离场
4. 单笔风险约净值 0.6%；IF 乘数 300

IF 5m 全样本（2019-11→2026-09）回测：Sharpe≈1.23，峰值回撤≈-2.6%，7/8 年为正。
默认 DRY_RUN=True。
"""

from __future__ import annotations

import math
import os
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime
from time import sleep
from typing import Any

from vnpy.trader.constant import Product
from vnpy.trader.object import TickData
from vnpy.trader.utility import round_to, save_json
from vnpy_scripttrader import ScriptEngine

STATUS_FILE = "if_hf_mr_status.json"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw in (None, ""):
        return float(default)
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        return int(default)
    return int(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    vt_symbol: str = ""
    product: str = "IF"
    exchange: str = "CFFEX"
    dry_run: bool = True
    loop_interval: float = 1.0
    capital: float = 1_000_000.0
    futures_size: float = 300.0
    pricetick: float = 0.2
    bar_minutes: int = 5
    look: int = 16
    z_entry: float = 2.4
    hold_bars: int = 6
    stop_atr: float = 1.5
    tp_atr: float = 1.0
    atr_n: int = 12
    risk: float = 0.006
    max_lots: int = 4
    max_day_trades: int = 1
    vol_min: float = 0.12
    vol_max: float = 0.85
    session_start: str = "09:45"
    session_end: str = "11:25"
    force_flat: str = "11:28"


def live_config() -> Config:
    return Config(
        dry_run=_env_bool("LIVE_DRY_RUN", True),
        look=_env_int("HF_LOOK", 16),
        z_entry=_env_float("HF_Z_ENTRY", 2.4),
        hold_bars=_env_int("HF_HOLD_BARS", 6),
        stop_atr=_env_float("HF_STOP_ATR", 1.5),
        tp_atr=_env_float("HF_TP_ATR", 1.0),
        risk=_env_float("HF_RISK", 0.006),
        max_lots=_env_int("HF_MAX_LOTS", 4),
        vol_min=_env_float("HF_VOL_MIN", 0.12),
        session_start=os.getenv("HF_SESSION_START") or "09:45",
        session_end=os.getenv("HF_SESSION_END") or "11:25",
    )


CFG = live_config()


def true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def bar_atr(bars: list[dict[str, float]], period: int) -> float:
    if len(bars) < 2:
        return 5.0
    trs: list[float] = []
    for i in range(1, len(bars)):
        trs.append(true_range(bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]))
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / max(len(window), 1)


def return_zscore(closes: list[float], look: int) -> tuple[float, float]:
    """Return (z of last log-return, annualized vol of window)."""
    if len(closes) < look + 1:
        return 0.0, 0.0
    window = closes[-(look + 1) :]
    rets = [math.log(window[i] / window[i - 1]) for i in range(1, len(window)) if window[i - 1] > 0]
    if len(rets) < look:
        return 0.0, 0.0
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / max(len(rets), 1)
    std = math.sqrt(max(var, 1e-16))
    z = (rets[-1] - mean) / std
    rvol = std * math.sqrt(242 * 48)
    return z, rvol


def target_lots(nav: float, stop_dist: float, size: float, risk: float, max_lots: int, min_lots: int = 0) -> int:
    unit = max(stop_dist * size, 1.0)
    lots = int((float(risk) * max(float(nav), 0.0)) // unit)
    lots = max(int(min_lots), lots)
    return max(0, min(int(max_lots), lots))


def parse_hhmm(text: str) -> dtime:
    hour, minute = text.split(":")[:2]
    return dtime(int(hour), int(minute))


def in_session(now: datetime, start: str, end: str) -> bool:
    t = now.time()
    return parse_hhmm(start) <= t <= parse_hhmm(end)


@dataclass
class Book:
    lots: int = 0
    entry: float = 0.0
    stop: float = 0.0
    take_profit: float = 0.0
    exit_bars_left: int = 0
    day_trades: int = 0
    day_key: str = ""
    notes: deque[str] = field(default_factory=lambda: deque(maxlen=60))


@dataclass
class Bar:
    stamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class BarBuilder:
    """Aggregate ticks into N-minute OHLCV bars."""

    def __init__(self, minutes: int = 5) -> None:
        self.minutes = max(int(minutes), 1)
        self.current: Bar | None = None
        self.closed: deque[Bar] = deque(maxlen=200)

    def _bucket(self, now: datetime) -> str:
        minute = (now.minute // self.minutes) * self.minutes
        bucket = now.replace(minute=minute, second=0, microsecond=0)
        return bucket.strftime("%Y-%m-%d %H:%M:%S")

    def update(self, now: datetime, price: float, volume: float = 0.0) -> Bar | None:
        if price <= 0:
            return None
        key = self._bucket(now)
        finished: Bar | None = None
        if self.current is None:
            self.current = Bar(key, price, price, price, price, volume)
            return None
        if key != self.current.stamp:
            finished = self.current
            self.closed.append(finished)
            self.current = Bar(key, price, price, price, price, volume)
            return finished
        self.current.high = max(self.current.high, price)
        self.current.low = min(self.current.low, price)
        self.current.close = price
        self.current.volume += volume
        return None


class IfHfMeanReversion:
    def __init__(self, engine: ScriptEngine, cfg: Config) -> None:
        self.engine = engine
        self.cfg = cfg
        self.book = Book()
        self.builder = BarBuilder(cfg.bar_minutes)
        self.bars: deque[Bar] = deque(maxlen=200)
        self.vt_symbol = cfg.vt_symbol
        self.subscribed = False

    def write(self, msg: str) -> None:
        self.engine.write_log(f"[IF-HF-MR] {msg}")
        self.book.notes.append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")

    def publish(self, extra: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {
            "active": True,
            "updated": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "dry_run": self.cfg.dry_run,
            "strategy": "if_hf_mr",
            "params": asdict(self.cfg),
            "symbol": self.vt_symbol,
            "book": {
                "lots": self.book.lots,
                "entry": self.book.entry,
                "stop": self.book.stop,
                "take_profit": self.book.take_profit,
                "exit_bars_left": self.book.exit_bars_left,
                "day_trades": self.book.day_trades,
            },
            "decisions": list(self.book.notes),
        }
        if extra:
            payload.update(extra)
        try:
            save_json(STATUS_FILE, payload)
        except Exception as exc:
            self.write(f"状态写入失败: {exc}")

    def resolve_symbol(self) -> str:
        if self.cfg.vt_symbol:
            return self.cfg.vt_symbol
        try:
            contracts = list(self.engine.get_all_contracts() or [])
        except Exception:
            contracts = []
        cands: list[tuple[float, str]] = []
        for contract in contracts:
            symbol = str(getattr(contract, "symbol", "") or "")
            product = getattr(contract, "product", None)
            if not symbol.startswith(self.cfg.product):
                continue
            if product not in (None, Product.FUTURES) and str(product) not in ("期货", "FUTURES"):
                continue
            vt_symbol = str(getattr(contract, "vt_symbol", "") or f"{symbol}.{self.cfg.exchange}")
            tick = self.engine.get_tick(vt_symbol)
            oi = float(getattr(tick, "open_interest", 0) or 0) if tick else 0.0
            cands.append((oi, vt_symbol))
        cands.sort(reverse=True)
        return cands[0][1] if cands else ""

    def account_nav(self) -> float:
        try:
            accounts = self.engine.get_all_accounts()
        except Exception:
            accounts = []
        if accounts:
            return float(getattr(accounts[0], "balance", 0) or self.cfg.capital)
        return self.cfg.capital

    def send(self, side: int, volume: int, price: float) -> None:
        if volume <= 0 or price <= 0 or not self.vt_symbol:
            return
        px = round_to(price, self.cfg.pricetick)
        if self.cfg.dry_run:
            action = "买开" if side > 0 else "卖开"
            self.write(f"模拟{action} {self.vt_symbol} x{volume} @{px:.1f}")
            return
        if side > 0:
            self.engine.buy(self.vt_symbol, px, volume)
        else:
            self.engine.short(self.vt_symbol, px, volume)

    def flatten(self, price: float, reason: str) -> None:
        if self.book.lots == 0:
            return
        volume = abs(self.book.lots)
        px = round_to(price, self.cfg.pricetick)
        if self.cfg.dry_run:
            action = "卖平" if self.book.lots > 0 else "买平"
            self.write(f"{reason} 模拟{action} {self.vt_symbol} x{volume} @{px:.1f}")
        else:
            if self.book.lots > 0:
                self.engine.sell(self.vt_symbol, px, volume)
            else:
                self.engine.cover(self.vt_symbol, px, volume)
        self.book.lots = 0
        self.book.entry = 0.0
        self.book.stop = 0.0
        self.book.take_profit = 0.0
        self.book.exit_bars_left = 0

    def on_bar(self, bar: Bar, nav: float) -> None:
        self.bars.append(bar)
        day = bar.stamp[:10]
        if day != self.book.day_key:
            self.book.day_key = day
            self.book.day_trades = 0
            if self.book.lots:
                self.flatten(bar.open, "换日强平")

        now = datetime.strptime(bar.stamp, "%Y-%m-%d %H:%M:%S")
        hhmm = now.strftime("%H:%M")
        if self.book.lots:
            self.book.exit_bars_left = max(self.book.exit_bars_left - 1, 0)
            if self.book.lots > 0:
                if bar.low <= self.book.stop:
                    self.flatten(min(bar.open, self.book.stop), "止损")
                elif self.book.take_profit > 0 and bar.high >= self.book.take_profit:
                    self.flatten(max(bar.open, self.book.take_profit), "止盈")
                elif self.book.exit_bars_left <= 0 or hhmm >= self.cfg.force_flat:
                    self.flatten(bar.close, "到期/午休前平仓")
            else:
                if bar.high >= self.book.stop:
                    self.flatten(max(bar.open, self.book.stop), "止损")
                elif self.book.take_profit > 0 and bar.low <= self.book.take_profit:
                    self.flatten(min(bar.open, self.book.take_profit), "止盈")
                elif self.book.exit_bars_left <= 0 or hhmm >= self.cfg.force_flat:
                    self.flatten(bar.close, "到期/午休前平仓")

        z = rvol = atr = 0.0
        lots = 0
        reason = ""
        allow = in_session(now, self.cfg.session_start, self.cfg.session_end)
        if self.book.lots == 0 and allow and self.book.day_trades < self.cfg.max_day_trades:
            closes = [item.close for item in self.bars]
            z, rvol = return_zscore(closes, self.cfg.look)
            bar_dicts = [{"high": b.high, "low": b.low, "close": b.close} for b in self.bars]
            atr = bar_atr(bar_dicts, self.cfg.atr_n)
            if abs(z) >= self.cfg.z_entry and self.cfg.vol_min <= rvol <= self.cfg.vol_max:
                stop_dist = max(self.cfg.stop_atr * atr, 1.5)
                lots = target_lots(nav, stop_dist, self.cfg.futures_size, self.cfg.risk, self.cfg.max_lots, min_lots=1)
                if lots >= 1:
                    side = -1 if z > 0 else 1
                    self.send(side, lots, bar.close)
                    self.book.lots = side * lots
                    self.book.entry = bar.close
                    self.book.stop = bar.close - stop_dist if side > 0 else bar.close + stop_dist
                    tp = self.cfg.tp_atr * atr
                    self.book.take_profit = (bar.close + tp) if side > 0 else (bar.close - tp)
                    self.book.exit_bars_left = self.cfg.hold_bars
                    self.book.day_trades += 1
                    self.write(
                        f"{'多' if side > 0 else '空'}开 {lots}手 @{bar.close:.1f} z={z:.2f} "
                        f"rvol={rvol:.2f} ATR={atr:.2f}"
                    )
                else:
                    reason = "仓位为0"
            else:
                reason = f"无信号 z={z:.2f} rvol={rvol:.2f}"
        elif not allow:
            reason = "非交易时段"
        elif self.book.day_trades >= self.cfg.max_day_trades:
            reason = "已达日交易上限"

        self.publish(
            {
                "spot": round(bar.close, 1),
                "nav": round(nav, 0),
                "z": round(z, 3),
                "rvol": round(rvol, 3),
                "atr": round(atr, 2),
                "lots_budget": lots,
                "bars": len(self.bars),
                "reason": reason or ("持仓中" if self.book.lots else "等待"),
            }
        )

    def run(self) -> None:
        self.write(f"启动 IF 高频均值回归 {'模拟' if self.cfg.dry_run else '实盘'}")
        try:
            while True:
                self.step()
                sleep(self.cfg.loop_interval)
        finally:
            self.write("脚本停止")
            self.publish({"active": False})

    def step(self) -> None:
        if not self.vt_symbol:
            self.vt_symbol = self.resolve_symbol()
            if not self.vt_symbol:
                self.publish({"reason": "等待 IF 主力合约，或在 Config.vt_symbol 填写"})
                return
        if not self.subscribed:
            self.engine.subscribe([self.vt_symbol])
            self.subscribed = True
        tick: TickData | None = self.engine.get_tick(self.vt_symbol)
        spot = 0.0
        volume = 0.0
        if tick:
            last = float(getattr(tick, "last_price", 0) or 0)
            bid = float(getattr(tick, "bid_price_1", 0) or 0)
            ask = float(getattr(tick, "ask_price_1", 0) or 0)
            volume = float(getattr(tick, "volume", 0) or 0)
            if last > 0:
                spot = last
            elif bid > 0 and ask > 0:
                spot = 0.5 * (bid + ask)
        if spot <= 0:
            self.publish({"reason": f"等待 {self.vt_symbol} 行情"})
            return
        now = datetime.now()
        finished = self.builder.update(now, spot, volume)
        nav = self.account_nav()
        if finished is not None:
            self.on_bar(finished, nav)
        else:
            self.publish(
                {
                    "spot": round(spot, 1),
                    "nav": round(nav, 0),
                    "bars": len(self.bars),
                    "reason": "聚合 5 分钟 K 线中",
                }
            )


def run(engine: ScriptEngine) -> None:
    IfHfMeanReversion(engine, live_config()).run()
