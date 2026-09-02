"""
SA 纯碱 CTA：唐奇安突破 + ATR 跟踪止损 + 波动率目标仓位（ScriptTrader）

商品期货主流做法的单品种落地：
1. 唐奇安 20 日突破开仓、10 日反向突破平仓（海龟系统一）
2. 2.5×ATR(20) 跟踪止损，单笔风险约净值 1.2%；默认只在日终检查（5 分钟回测盘中止损会亏）
3. 按 20 日实现波动把仓位缩放到约 12% 年化波动
4. 多空都做，趋势市持有、震荡市频繁止损但单笔小

默认交易 SA 主力期货。连接 CTP 后在脚本页启动。DRY_RUN=True。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from time import sleep

from vnpy.trader.constant import Product
from vnpy.trader.object import TickData
from vnpy.trader.utility import round_to, save_json
from vnpy_scripttrader import ScriptEngine

STATUS_FILE = "sa_cta_trend_status.json"


def true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def wilder_atr(trs: list[float], period: int) -> float:
    if len(trs) < period:
        return float(np_mean(trs)) if trs else 0.0
    atr = sum(trs[:period]) / period
    for value in trs[period:]:
        atr = (atr * (period - 1) + value) / period
    return atr


def np_mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def realized_vol(closes: list[float], lookback: int = 20) -> float:
    if len(closes) < lookback + 1:
        return 0.30
    window = closes[-(lookback + 1) :]
    rets = [math.log(window[i] / window[i - 1]) for i in range(1, len(window)) if window[i - 1] > 0]
    if len(rets) < 5:
        return 0.30
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / max(len(rets) - 1, 1)
    return max(0.08, min(1.20, math.sqrt(var) * math.sqrt(242)))


def channel(values: list[float], period: int, want_max: bool) -> float:
    window = values[-period:] if len(values) >= period else values
    if not window:
        return 0.0
    return max(window) if want_max else min(window)


def target_lots(
    nav: float,
    atr: float,
    size: float,
    risk: float,
    vol_tgt: float,
    rvol: float,
    max_lots: int,
) -> int:
    unit = max(atr * size, 1.0)
    lots = int((float(risk) * max(float(nav), 0.0)) // unit)
    if vol_tgt > 0:
        lots = int(lots * min(2.0, max(0.35, float(vol_tgt) / max(float(rvol), 0.08))))
    return max(0, min(int(max_lots), lots))


@dataclass
class Config:
    vt_symbol: str = ""
    product: str = "SA"
    exchange: str = "CZCE"
    dry_run: bool = True
    loop_interval: float = 2.0
    capital: float = 1_000_000.0
    futures_size: float = 20.0
    pricetick: float = 1.0
    entry_n: int = 20
    exit_n: int = 10
    atr_n: int = 20
    atr_stop: float = 2.5
    risk: float = 0.012
    vol_tgt: float = 0.12
    vol_lookback: int = 20
    max_lots: int = 80
    # 5 分钟回测：盘中突破开仓为正期望，盘中 ATR/通道止损会把期望打没；默认日终才平仓。
    intraday_stop: bool = False


CFG = Config()


@dataclass
class Book:
    lots: int = 0
    entry: float = 0.0
    extreme: float = 0.0
    stop: float = 0.0
    notes: deque[str] = field(default_factory=lambda: deque(maxlen=40))


class SaCtaTrend:
    def __init__(self, engine: ScriptEngine, cfg: Config) -> None:
        self.engine = engine
        self.cfg = cfg
        self.book = Book()
        self.day_highs: deque[float] = deque(maxlen=80)
        self.day_lows: deque[float] = deque(maxlen=80)
        self.day_closes: deque[float] = deque(maxlen=80)
        self.day_trs: deque[float] = deque(maxlen=80)
        self.today: date | None = None
        self.session_open = 0.0
        self.session_high = 0.0
        self.session_low = 0.0
        self.last_spot = 0.0
        self.vt_symbol = cfg.vt_symbol
        self.subscribed = False

    def write(self, msg: str) -> None:
        self.engine.write_log(f"[SA-CTA] {msg}")
        self.book.notes.append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")

    def publish(self, extra: dict | None = None) -> None:
        payload = {
            "active": True,
            "updated": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "dry_run": self.cfg.dry_run,
            "params": asdict(self.cfg),
            "symbol": self.vt_symbol,
            "book": {
                "lots": self.book.lots,
                "entry": self.book.entry,
                "extreme": self.book.extreme,
                "stop": self.book.stop,
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
        cands = []
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
        self.book.extreme = 0.0
        self.book.stop = 0.0

    def update_daily(self, spot: float, today: date) -> None:
        if self.today is None:
            self.today = today
            self.session_open = self.session_high = self.session_low = spot
            return
        if today != self.today:
            if self.last_spot > 0:
                prev = self.day_closes[-1] if self.day_closes else self.last_spot
                self.day_highs.append(self.session_high)
                self.day_lows.append(self.session_low)
                self.day_closes.append(self.last_spot)
                self.day_trs.append(true_range(self.session_high, self.session_low, prev))
            self.today = today
            self.session_open = self.session_high = self.session_low = spot
            return
        self.session_high = max(self.session_high, spot)
        self.session_low = min(self.session_low, spot) if self.session_low else spot

    def run(self) -> None:
        self.write(f"启动 SA CTA {'模拟' if self.cfg.dry_run else '实盘'}")
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
                self.publish({"reason": "等待 SA 主力合约，或在 Config.vt_symbol 填写"})
                return
        if not self.subscribed:
            self.engine.subscribe([self.vt_symbol])
            self.subscribed = True
        tick: TickData | None = self.engine.get_tick(self.vt_symbol)
        spot = 0.0
        if tick:
            last = float(getattr(tick, "last_price", 0) or 0)
            bid = float(getattr(tick, "bid_price_1", 0) or 0)
            ask = float(getattr(tick, "ask_price_1", 0) or 0)
            if last > 0:
                spot = last
            elif bid > 0 and ask > 0:
                spot = 0.5 * (bid + ask)
        if spot <= 0:
            self.publish({"reason": f"等待 {self.vt_symbol} 行情"})
            return
        self.last_spot = spot
        today = datetime.now().date()
        self.update_daily(spot, today)

        highs = list(self.day_highs)
        lows = list(self.day_lows)
        closes = list(self.day_closes)
        if len(highs) < self.cfg.entry_n:
            self.publish({"spot": spot, "reason": f"日线不足 {len(highs)}/{self.cfg.entry_n}"})
            return

        up = channel(highs, self.cfg.entry_n, True)
        dn = channel(lows, self.cfg.entry_n, False)
        exit_up = channel(highs, self.cfg.exit_n, True)
        exit_dn = channel(lows, self.cfg.exit_n, False)
        atr = wilder_atr(list(self.day_trs), self.cfg.atr_n)
        rvol = realized_vol(closes, self.cfg.vol_lookback)
        nav = self.account_nav()

        now = datetime.now()
        hhmm = now.strftime("%H:%M")
        session_end = "14:55" <= hhmm <= "15:10" or hhmm >= "22:50"
        allow_exit = self.cfg.intraday_stop or session_end
        if self.book.lots > 0:
            self.book.extreme = max(self.book.extreme, spot)
            self.book.stop = self.book.extreme - self.cfg.atr_stop * atr
            if allow_exit and (spot <= self.book.stop or spot < exit_dn):
                reason = "ATR止损" if spot <= self.book.stop else "10日下轨平多"
                self.flatten(spot, reason)
        elif self.book.lots < 0:
            self.book.extreme = min(self.book.extreme or spot, spot)
            self.book.stop = self.book.extreme + self.cfg.atr_stop * atr
            if allow_exit and (spot >= self.book.stop or spot > exit_up):
                reason = "ATR止损" if spot >= self.book.stop else "10日上轨平空"
                self.flatten(spot, reason)

        lots = target_lots(
            nav, atr, self.cfg.futures_size, self.cfg.risk, self.cfg.vol_tgt, rvol, self.cfg.max_lots
        )
        if self.book.lots == 0 and lots >= 1:
            if spot > up:
                self.send(1, lots, spot)
                self.book.lots = lots
                self.book.entry = self.book.extreme = spot
                self.book.stop = spot - self.cfg.atr_stop * atr
                self.write(f"多开 {lots} 手 @{spot:.1f} 上轨={up:.1f} ATR={atr:.1f}")
            elif spot < dn:
                self.send(-1, lots, spot)
                self.book.lots = -lots
                self.book.entry = self.book.extreme = spot
                self.book.stop = spot + self.cfg.atr_stop * atr
                self.write(f"空开 {lots} 手 @{spot:.1f} 下轨={dn:.1f} ATR={atr:.1f}")

        self.publish(
            {
                "spot": round(spot, 1),
                "nav": round(nav, 0),
                "up": round(up, 1),
                "dn": round(dn, 1),
                "exit_up": round(exit_up, 1),
                "exit_dn": round(exit_dn, 1),
                "atr": round(atr, 2),
                "rvol": round(rvol, 3),
                "lots_budget": lots,
            }
        )


def run(engine: ScriptEngine) -> None:
    SaCtaTrend(engine, CFG).run()
