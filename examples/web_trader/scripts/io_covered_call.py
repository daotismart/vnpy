"""
备兑看涨（Covered Call）+ 盘口错价收割（ScriptTrader）

结构：
1. 持有 IF 多头作备兑底仓
2. 仅在看涨期权买一价相对 Black-76 理论价偏贵（错价）时卖出 Call 收割权利金
3. 覆盖比例按乘数：每 3 手 IO Call 配 1 手 IF（300/100）
4. 错价消失 / 权利金收回 / Delta 恶化 / 到期临近则买回 Call；底仓可保留或同步平掉

与「电子眼裸吃单」区别：始终用期货多头覆盖上行义务，避免裸卖风险。
默认 DRY_RUN=True。使用前连接 CTP，期权页初始化 IO.CFFEX。
"""

from __future__ import annotations

import math
import os
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime
from time import sleep, time as wall_time

from vnpy.trader.constant import OptionType, Product
from vnpy.trader.object import TickData
from vnpy.trader.utility import round_to, save_json
from vnpy_optionmaster.base import APP_NAME, OptionData, PortfolioData
from vnpy_optionmaster.engine import OptionEngine
from vnpy_optionmaster.pricing.black_76 import calculate_delta, calculate_price
from vnpy_scripttrader import ScriptEngine

STATUS_FILE = "io_covered_call_status.json"
RATE = 0.02


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
    portfolio_name: str = "IO.CFFEX"
    chain_symbol: str = ""
    dry_run: bool = True
    loop_interval: float = 0.5
    stale_tick_sec: float = 6.0

    option_size: float = 100.0
    futures_size: float = 300.0
    pricetick: float = 0.2
    cover_ratio: int = 3  # IO 手数 / IF 手数

    # 选腿：次月附近、轻度虚值看涨
    min_delta: float = 0.15
    max_delta: float = 0.35
    min_dte: int = 10
    max_dte: int = 45
    atm_strikes: int = 8

    # 错价：买一价相对 theo 至少贵 edge_ticks 跳
    edge_ticks: float = 2.0
    min_credit: float = 0.6
    theo_iv_floor: float = 0.12
    theo_iv_cap: float = 0.55
    use_pricing_impv: bool = True

    # 仓位
    call_lots: int = 3
    max_call_lots: int = 9
    capital: float = 1_000_000.0
    risk_cap: float = 0.08  # 以备兑组合近似风险占净值

    # 出场
    take_profit: float = 0.45  # 买回成本 ≤ 入场权利金 * take_profit
    delta_stop: float = 0.55
    roll_dte: int = 7
    flat_eod: bool = False  # True=日内策略，尾盘平掉 Call（可留 IF）
    eod_hhmm: str = "14:50"
    stop_if_pct: float = 0.02  # IF 相对开仓价回撤 2% 则整组离场

    max_quotes: int = 12


def live_config() -> Config:
    return Config(
        dry_run=_env_bool("LIVE_DRY_RUN", True),
        portfolio_name=os.getenv("LIVE_PORTFOLIOS") or "IO.CFFEX",
        edge_ticks=_env_float("CC_EDGE_TICKS", 2.0),
        call_lots=_env_int("CC_CALL_LOTS", 3),
        max_call_lots=_env_int("CC_MAX_CALL_LOTS", 9),
        take_profit=_env_float("CC_TAKE_PROFIT", 0.50),
        delta_stop=_env_float("CC_DELTA_STOP", 0.55),
        flat_eod=_env_bool("CC_FLAT_EOD", True),
        min_delta=_env_float("CC_MIN_DELTA", 0.12),
        max_delta=_env_float("CC_MAX_DELTA", 0.40),
        edge_ticks=_env_float("CC_EDGE_TICKS", 2.5),
    )


CFG = live_config()


def cffex_session(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (dtime(9, 25) <= t <= dtime(11, 31)) or (dtime(12, 58) <= t <= dtime(15, 1))


def cover_futures_lots(call_lots: int, cover_ratio: int) -> int:
    ratio = max(int(cover_ratio), 1)
    return max(1, int(math.ceil(abs(call_lots) / ratio)))


@dataclass
class Book:
    call_symbol: str = ""
    fut_symbol: str = ""
    call_lots: int = 0  # short call = negative in net sense; we store abs sold as positive short
    fut_lots: int = 0
    entry_credit: float = 0.0
    entry_fut: float = 0.0
    entry_strike: float = 0.0
    notes: deque[str] = field(default_factory=lambda: deque(maxlen=60))


class IoCoveredCall:
    def __init__(self, engine: ScriptEngine, cfg: Config) -> None:
        self.engine = engine
        self.cfg = cfg
        self.book = Book()
        self.loop = 0
        self.subscribed: set[str] = set()

    def write(self, msg: str) -> None:
        self.engine.write_log(f"[CC-CALL] {msg}")
        self.book.notes.append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")

    def publish(self, extra: dict | None = None) -> None:
        payload = {
            "active": True,
            "updated": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "strategy": "io_covered_call",
            "dry_run": self.cfg.dry_run,
            "params": asdict(self.cfg),
            "book": {
                "call_symbol": self.book.call_symbol,
                "fut_symbol": self.book.fut_symbol,
                "call_lots": self.book.call_lots,
                "fut_lots": self.book.fut_lots,
                "entry_credit": self.book.entry_credit,
                "entry_fut": self.book.entry_fut,
                "entry_strike": self.book.entry_strike,
            },
            "decisions": list(self.book.notes),
        }
        if extra:
            payload.update(extra)
        try:
            save_json(STATUS_FILE, payload)
        except Exception as exc:
            self.write(f"状态写入失败: {exc}")

    def option_engine(self) -> OptionEngine | None:
        engine = self.engine.main_engine.get_engine(APP_NAME)
        return engine if isinstance(engine, OptionEngine) else None

    def get_portfolio(self) -> PortfolioData | None:
        opt = self.option_engine()
        if not opt:
            return None
        return opt.portfolios.get(self.cfg.portfolio_name)

    def get_chain(self, portfolio: PortfolioData):
        symbols = sorted(set(getattr(portfolio, "_chains", {}) or {}) | set(portfolio.chains or {}))
        chain_symbol = self.cfg.chain_symbol or (symbols[0] if symbols else "")
        if not chain_symbol:
            return None, ""
        chain = (portfolio.chains.get(chain_symbol) if portfolio.chains else None) or (
            getattr(portfolio, "_chains", {}) or {}
        ).get(chain_symbol)
        return chain, chain_symbol

    def resolve_futures(self, chain) -> str:
        underlying = getattr(chain, "underlying", None)
        if underlying is not None:
            vt = str(getattr(underlying, "vt_symbol", "") or "")
            if vt:
                return vt
        # fallback: IF main by OI
        try:
            contracts = list(self.engine.get_all_contracts() or [])
        except Exception:
            contracts = []
        cands: list[tuple[float, str]] = []
        for contract in contracts:
            symbol = str(getattr(contract, "symbol", "") or "")
            product = getattr(contract, "product", None)
            if not symbol.startswith("IF"):
                continue
            if product not in (None, Product.FUTURES) and str(product) not in ("期货", "FUTURES"):
                continue
            vt_symbol = str(getattr(contract, "vt_symbol", "") or f"{symbol}.CFFEX")
            tick = self.engine.get_tick(vt_symbol)
            oi = float(getattr(tick, "open_interest", 0) or 0) if tick else 0.0
            cands.append((oi, vt_symbol))
        cands.sort(reverse=True)
        return cands[0][1] if cands else ""

    def ensure_sub(self, vt_symbol: str) -> None:
        if not vt_symbol or vt_symbol in self.subscribed:
            return
        self.engine.subscribe([vt_symbol])
        self.subscribed.add(vt_symbol)

    def spot_from_tick(self, tick: TickData | None) -> float:
        if not tick:
            return 0.0
        last = float(getattr(tick, "last_price", 0) or 0)
        bid = float(getattr(tick, "bid_price_1", 0) or 0)
        ask = float(getattr(tick, "ask_price_1", 0) or 0)
        if last > 0:
            return last
        if bid > 0 and ask > 0:
            return 0.5 * (bid + ask)
        return max(bid, ask, 0.0)

    def theo_call(self, spot: float, strike: float, t: float, iv: float) -> tuple[float, float]:
        iv = min(max(iv, self.cfg.theo_iv_floor), self.cfg.theo_iv_cap)
        t = max(t, 1.0 / 365.0)
        price = float(calculate_price(spot, strike, RATE, t, iv, 1))
        delta = float(calculate_delta(spot, strike, RATE, t, iv, 1))
        return max(price, self.cfg.pricetick), delta

    def send_fut(self, side: int, volume: int, price: float) -> None:
        if volume <= 0 or not self.book.fut_symbol:
            return
        px = round_to(price, self.cfg.pricetick)
        if self.cfg.dry_run:
            self.write(f"模拟{'买开IF' if side > 0 else '卖平IF'} {self.book.fut_symbol} x{volume} @{px:.1f}")
            return
        if side > 0:
            self.engine.buy(self.book.fut_symbol, px, volume)
        else:
            self.engine.sell(self.book.fut_symbol, px, volume)

    def send_call(self, sell: bool, volume: int, price: float) -> None:
        if volume <= 0 or not self.book.call_symbol:
            return
        px = round_to(price, self.cfg.pricetick)
        if self.cfg.dry_run:
            self.write(f"模拟{'卖开Call' if sell else '买平Call'} {self.book.call_symbol} x{volume} @{px:.1f}")
            return
        if sell:
            self.engine.short(self.book.call_symbol, px, volume)
        else:
            self.engine.cover(self.book.call_symbol, px, volume)

    def flatten_call(self, price: float, reason: str) -> None:
        if self.book.call_lots <= 0:
            return
        self.send_call(False, self.book.call_lots, price)
        self.write(f"{reason} 平Call {self.book.call_lots}手 @{price:.1f}")
        self.book.call_lots = 0
        self.book.call_symbol = ""
        self.book.entry_credit = 0.0
        self.book.entry_strike = 0.0

    def flatten_fut(self, price: float, reason: str) -> None:
        if self.book.fut_lots <= 0:
            return
        self.send_fut(-1, self.book.fut_lots, price)
        self.write(f"{reason} 平IF {self.book.fut_lots}手 @{price:.1f}")
        self.book.fut_lots = 0
        self.book.entry_fut = 0.0

    def is_call(self, option: OptionData) -> bool:
        ot = getattr(option, "option_type", None)
        if ot == OptionType.CALL:
            return True
        token = str(ot or "").upper()
        return "CALL" in token or "看涨" in str(ot or "") or token in {"C", "1"}

    def select_rich_call(self, chain, spot: float, now: datetime) -> dict | None:
        today = now.date()
        best = None
        edge_cash = self.cfg.edge_ticks * self.cfg.pricetick
        for option in (chain.options or {}).values():
            if not self.is_call(option):
                continue
            tick: TickData | None = getattr(option, "tick", None)
            if not tick:
                continue
            bid = float(getattr(tick, "bid_price_1", 0) or 0)
            ask = float(getattr(tick, "ask_price_1", 0) or 0)
            if bid <= 0:
                continue
            strike = float(getattr(option, "strike_price", 0) or 0)
            if strike <= 0:
                continue
            expiry = getattr(option, "option_expiry", None) or getattr(option, "expiry", None)
            if expiry is None:
                continue
            expiry_d = expiry.date() if hasattr(expiry, "date") else expiry
            dte = (expiry_d - today).days
            if not (self.cfg.min_dte <= dte <= self.cfg.max_dte):
                continue
            t = max(dte, 1) / 365.0
            iv = 0.0
            if self.cfg.use_pricing_impv:
                iv = float(getattr(option, "pricing_impv", 0) or 0) or float(getattr(option, "mid_impv", 0) or 0)
            if iv <= 0:
                iv = max(self.cfg.theo_iv_floor, 0.18)
            theo, delta = self.theo_call(spot, strike, t, iv)
            ref = float(getattr(option, "theo_price", 0) or 0)
            if ref > 0:
                theo = ref
                delta = abs(float(getattr(option, "theo_delta", delta) or delta))
            if not (self.cfg.min_delta <= abs(delta) <= self.cfg.max_delta):
                continue
            if bid < self.cfg.min_credit:
                continue
            edge = bid - theo
            if edge < edge_cash:
                continue
            score = edge / max(theo, self.cfg.pricetick)
            cand = {
                "option": option,
                "vt_symbol": option.vt_symbol,
                "strike": strike,
                "bid": bid,
                "ask": ask,
                "theo": theo,
                "delta": abs(delta),
                "edge": edge,
                "dte": dte,
                "score": score,
            }
            if best is None or cand["score"] > best["score"]:
                best = cand
        return best

    def run(self) -> None:
        self.write(f"启动备兑看涨错价收割 {'模拟' if self.cfg.dry_run else '实盘'}")
        try:
            while self.engine.strategy_active:
                try:
                    self.step()
                except Exception as exc:
                    self.write(f"循环异常: {exc}")
                    self.publish({"error": str(exc)})
                sleep(self.cfg.loop_interval)
        finally:
            self.publish({"active": False})
            self.write("脚本停止")

    def step(self) -> None:
        self.loop += 1
        now = datetime.now()
        if not cffex_session(now):
            self.publish({"reason": "非交易时段"})
            return
        portfolio = self.get_portfolio()
        if not portfolio:
            self.publish({"reason": f"等待组合 {self.cfg.portfolio_name} 初始化"})
            return
        chain, chain_symbol = self.get_chain(portfolio)
        if not chain:
            self.publish({"reason": "期权链为空"})
            return
        fut = self.resolve_futures(chain)
        if not fut:
            self.publish({"reason": "找不到 IF 标的"})
            return
        self.book.fut_symbol = fut
        self.ensure_sub(fut)
        fut_tick = self.engine.get_tick(fut)
        spot = self.spot_from_tick(fut_tick)
        if spot <= 0:
            # try chain underlying mid
            underlying = getattr(chain, "underlying", None)
            spot = float(getattr(underlying, "mid_price", 0) or 0) if underlying else 0.0
        if spot <= 0:
            self.publish({"reason": "等待 IF 行情"})
            return

        # manage open covered call
        if self.book.call_lots > 0 and self.book.call_symbol:
            self.ensure_sub(self.book.call_symbol)
            call_tick = self.engine.get_tick(self.book.call_symbol)
            ask = float(getattr(call_tick, "ask_price_1", 0) or 0) if call_tick else 0.0
            bid = float(getattr(call_tick, "bid_price_1", 0) or 0) if call_tick else 0.0
            mid = 0.5 * (bid + ask) if bid > 0 and ask > 0 else max(ask, bid)
            # find option object for delta
            option = (chain.options or {}).get(self.book.call_symbol)
            delta = abs(float(getattr(option, "theo_delta", 0) or 0)) if option else 0.0
            hhmm = now.strftime("%H:%M")
            take_profit = (
                self.book.entry_credit > 0
                and mid > 0
                and mid <= self.cfg.take_profit * self.book.entry_credit
            )
            delta_hit = delta >= self.cfg.delta_stop
            dte_hit = False
            if option is not None:
                expiry = getattr(option, "option_expiry", None) or getattr(option, "expiry", None)
                if expiry is not None:
                    expiry_d = expiry.date() if hasattr(expiry, "date") else expiry
                    dte_hit = (expiry_d - now.date()).days <= self.cfg.roll_dte
            fut_stop = self.book.entry_fut > 0 and spot <= self.book.entry_fut * (1.0 - self.cfg.stop_if_pct)
            eod = self.cfg.flat_eod and hhmm >= self.cfg.eod_hhmm
            if take_profit or delta_hit or dte_hit or fut_stop or eod:
                px = ask if ask > 0 else mid
                reason = (
                    "止盈"
                    if take_profit
                    else (
                        "Delta止损"
                        if delta_hit
                        else ("移仓" if dte_hit else ("IF止损" if fut_stop else "尾盘平仓"))
                    )
                )
                self.flatten_call(px or spot * 0.01, reason)
                if fut_stop or eod:
                    self.flatten_fut(spot, reason)
                self.publish(
                    {
                        "spot": round(spot, 1),
                        "chain": chain_symbol,
                        "reason": reason,
                        "mid": round(mid, 2),
                        "delta": round(delta, 3),
                    }
                )
                return

        # entry: only when flat on short call
        if self.book.call_lots > 0:
            self.publish({"spot": round(spot, 1), "chain": chain_symbol, "reason": "持有备兑Call"})
            return

        rich = self.select_rich_call(chain, spot, now)
        if rich is None:
            self.publish({"spot": round(spot, 1), "chain": chain_symbol, "reason": "无足够错价的看涨卖单"})
            return

        call_lots = min(self.cfg.call_lots, self.cfg.max_call_lots)
        fut_lots = cover_futures_lots(call_lots, self.cfg.cover_ratio)
        # open IF first (cover), then sell call
        if self.book.fut_lots < fut_lots:
            need = fut_lots - self.book.fut_lots
            self.send_fut(1, need, spot)
            self.book.fut_lots += need
            if self.book.entry_fut <= 0:
                self.book.entry_fut = spot
        self.book.call_symbol = rich["vt_symbol"]
        self.ensure_sub(self.book.call_symbol)
        self.send_call(True, call_lots, rich["bid"])
        self.book.call_lots = call_lots
        self.book.entry_credit = float(rich["bid"])
        self.book.entry_strike = float(rich["strike"])
        self.write(
            f"备兑开仓 IF{self.book.fut_lots} + 卖Call{call_lots} {rich['vt_symbol']} "
            f"K={rich['strike']:.0f} bid={rich['bid']:.1f} theo={rich['theo']:.1f} "
            f"edge={rich['edge']:.1f} Δ={rich['delta']:.2f} DTE={rich['dte']}"
        )
        self.publish(
            {
                "spot": round(spot, 1),
                "chain": chain_symbol,
                "reason": "已开备兑",
                "edge": round(rich["edge"], 2),
                "theo": round(rich["theo"], 2),
                "bid": round(rich["bid"], 2),
                "delta": round(rich["delta"], 3),
            }
        )


def run(engine: ScriptEngine) -> None:
    IoCoveredCall(engine, live_config()).run()
