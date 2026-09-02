"""
工业改良版 Avellaneda-Stoikov 期权做市策略（ScriptTrader）

相对经典 AS / OptionMaster 电子眼的改进：
1. 以模型价与市场中间价混合作为公允价，避免被过时成交价带偏
2. AS 保留价按库存偏斜，最优价差叠加 Vega/Gamma/虚值宽度
3. 库存用手数 + Delta/Gamma/Vega 风险加权
4. 不交叉盘口、涨跌停保护、买卖一档数量约束
5. 最小挂单存活时间，减少频繁改价
6. 组合级 Delta/Gamma/Vega/单合约仓位熔断；触发后只平仓侧报价
7. 成交冷却、过期行情撤单
8. 默认模拟报价（DRY_RUN=True），确认参数后再改 False

使用前：
1. 连接 CTP，在「期权」页初始化组合
2. 修改下方 CONFIG
3. 「脚本」页启动本文件；停止脚本时会撤销全部挂单
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from time import sleep, time

from vnpy.trader.object import OrderData, TickData
from vnpy.trader.utility import ceil_to, floor_to, round_to, save_json
from vnpy_optionmaster.base import APP_NAME, OptionData, PortfolioData
from vnpy_optionmaster.engine import OptionEngine
from vnpy_scripttrader import ScriptEngine

STATUS_FILE = "as_option_mm_status.json"


# ---------------------------------------------------------------------------
# 参数：按交易品种修改
# ---------------------------------------------------------------------------
@dataclass
class Config:
    portfolio_name: str = "IO.CFFEX"
    chain_symbol: str = ""              # 空则自动选第一条链
    atm_strikes: int = 5                # ATM 上下各 N 档；0 表示只按 Delta 过滤
    min_unit_delta: float = 0.15        # 单位 Delta 下限（过虚值不报）
    max_unit_delta: float = 0.85        # 单位 Delta 上限（过实值不报）

    dry_run: bool = True                # True 只记日志不下单
    loop_interval: float = 0.8          # 轮询秒
    stale_tick_sec: float = 8.0         # 行情过期撤单
    log_every: int = 8                  # 每隔多少轮打印一次簿记

    # AS 核心
    gamma: float = 0.08                 # 风险厌恶，越大越怕库存
    kappa: float = 1.4                  # 订单到达强度，越大价差越窄
    sigma: float = 0.22                 # 标的年化波动（无实时估计时使用）
    tau_days: float = 0.15              # 做市视野（交易日），对应 AS 的 T-t
    theo_weight: float = 0.65           # 公允价中模型价权重，其余为市场中间价

    # 价差加宽
    min_spread_ticks: int = 2
    max_spread_ticks: int = 40
    vol_spread: float = 0.015           # Vega 价差：vol_spread * vega / size
    gamma_spread_ticks: float = 1.0     # 按相对 ATM Gamma 加宽
    otm_spread_ticks: float = 1.5       # |Δ-0.5| 越大越宽
    inventory_spread_ticks: float = 1.0 # 库存越大越宽

    # 数量
    quote_volume: int = 1
    min_volume: int = 1
    max_order_size: int = 5
    size_skew: float = 0.6              # 库存倾斜报单量，0~1
    max_pos: int = 10                   # 单合约净仓上限（手）

    # 改价
    requote_ticks: int = 1              # 目标价偏离挂单价达到 N 跳才撤改
    min_quote_life: float = 1.2         # 最短挂单存活秒
    fill_cooldown: float = 2.0          # 成交后冷却秒

    # 组合风控（0 表示不启用该项）
    max_book_lots: int = 80
    max_portfolio_delta: float = 0.0    # 组合 pos_delta 绝对值上限，0 为不限制
    max_portfolio_gamma: float = 0.0
    max_portfolio_vega: float = 0.0
    flatten_inventory: float = 0.75     # 库存超过 max_pos 该比例后只挂减仓边


CFG = Config()


@dataclass
class QuotePlan:
    vt_symbol: str
    strike: float = 0.0
    option_type: str = ""
    bid: float = 0.0
    ask: float = 0.0
    bid_volume: int = 0
    ask_volume: int = 0
    market_bid: float = 0.0
    market_ask: float = 0.0
    last_price: float = 0.0
    theo: float = 0.0
    mid: float = 0.0
    reservation: float = 0.0
    spread: float = 0.0
    net_pos: int = 0
    unit_delta: float = 0.0
    iv: float = 0.0
    spread_driver: str = ""
    spread_as: float = 0.0
    spread_vega: float = 0.0
    spread_gamma: float = 0.0
    spread_otm: float = 0.0
    spread_inv: float = 0.0
    allow_bid: bool = False
    allow_ask: bool = False
    quoting: bool = False
    action: str = ""
    reason: str = ""


@dataclass
class WorkingQuote:
    bid_orderid: str = ""
    ask_orderid: str = ""
    bid_price: float = 0.0
    ask_price: float = 0.0
    last_replace: float = 0.0
    last_fill: float = 0.0
    last_pos: int = 0


class AsOptionMaker:
    """组合级 AS 期权做市。"""

    def __init__(self, engine: ScriptEngine, cfg: Config) -> None:
        self.engine = engine
        self.cfg = cfg
        self.working: dict[str, WorkingQuote] = {}
        self.loop_count = 0
        self.halted = False
        self.halt_reason = ""
        self.decisions: deque[str] = deque(maxlen=40)
        self.chain_symbol = ""

    def write(self, msg: str) -> None:
        self.engine.write_log(f"[AS-MM] {msg}")

    def note(self, msg: str, to_log: bool = False) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"{stamp} {msg}"
        self.decisions.append(line)
        if to_log:
            self.write(msg)

    def publish(self, extra: dict | None = None) -> None:
        payload = {
            "active": True,
            "updated": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "loop": self.loop_count,
            "dry_run": self.cfg.dry_run,
            "params": asdict(self.cfg),
            "chain_symbol": self.chain_symbol,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "decisions": list(self.decisions),
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

    def run(self) -> None:
        self.write(
            f"启动 {self.cfg.portfolio_name} "
            f"{'模拟报价' if self.cfg.dry_run else '实盘报价'} "
            f"gamma={self.cfg.gamma} kappa={self.cfg.kappa} sigma={self.cfg.sigma} "
            f"tau={self.cfg.tau_days}d theo_w={self.cfg.theo_weight} max_pos={self.cfg.max_pos}"
        )
        self.note(
            f"参数 gamma={self.cfg.gamma} kappa={self.cfg.kappa} sigma={self.cfg.sigma} "
            f"min_spread={self.cfg.min_spread_ticks}tick vol_spread={self.cfg.vol_spread}",
            to_log=True,
        )
        self.publish({"quotes": [], "portfolio": {}, "error": ""})
        try:
            while self.engine.strategy_active:
                try:
                    self.on_loop()
                except Exception as exc:
                    self.write(f"循环异常: {exc}")
                    self.note(f"循环异常: {exc}")
                    self.publish({"error": str(exc), "quotes": []})
                sleep(self.cfg.loop_interval)
        finally:
            self.cancel_all()
            self.publish({"active": False, "quotes": [], "error": "已停止"})
            self.write("已停止并撤销全部挂单")

    def on_loop(self) -> None:
        portfolio = self.get_portfolio()
        if not portfolio:
            msg = f"找不到组合 {self.cfg.portfolio_name}，请先在期权页初始化"
            if self.loop_count % self.cfg.log_every == 0:
                self.write(msg)
                self.note(msg)
            self.publish({"error": msg, "quotes": [], "portfolio": {}})
            self.loop_count += 1
            return

        portfolio.calculate_pos_greeks()
        chain, chain_symbol = self.get_chain(portfolio)
        self.chain_symbol = chain_symbol
        if not chain:
            msg = "期权链为空"
            if self.loop_count % self.cfg.log_every == 0:
                self.write(msg)
                self.note(msg)
            self.publish({"error": msg, "quotes": [], "portfolio": self.portfolio_view(portfolio)})
            self.loop_count += 1
            return

        self.halted, self.halt_reason = self.check_portfolio_risk(portfolio)
        options = self.select_options(chain)
        plans = [self.build_plan(option, chain) for option in options]
        quoting = [plan for plan in plans if plan.quoting]
        active = {plan.vt_symbol for plan in quoting}
        for vt_symbol in list(self.working):
            if vt_symbol not in active:
                self.cancel_symbol(vt_symbol)
                self.note(f"撤出报价 {vt_symbol}")

        for plan in quoting:
            self.sync_orders(plan)

        self.loop_count += 1
        error = self.halt_reason if self.halted else ""
        self.publish(
            {
                "error": error,
                "quotes": [asdict(plan) for plan in plans],
                "portfolio": self.portfolio_view(portfolio),
                "quote_count": len(quoting),
                "watch_count": len(plans),
            }
        )
        if self.loop_count % self.cfg.log_every == 0 and plans:
            sample = quoting[len(quoting) // 2] if quoting else plans[0]
            self.write(
                f"{chain_symbol} 监控{len(plans)} 报价{len(quoting)} "
                f"Δ={portfolio.pos_delta:.1f} Γ={portfolio.pos_gamma:.4f} "
                f"{sample.vt_symbol} {sample.bid}/{sample.ask} {sample.action} {sample.reason}"
            )

    def portfolio_view(self, portfolio: PortfolioData) -> dict:
        return {
            "name": getattr(portfolio, "name", self.cfg.portfolio_name),
            "net_pos": portfolio.net_pos,
            "pos_delta": round(float(portfolio.pos_delta or 0), 4),
            "pos_gamma": round(float(portfolio.pos_gamma or 0), 6),
            "pos_vega": round(float(portfolio.pos_vega or 0), 4),
            "pos_theta": round(float(portfolio.pos_theta or 0), 4),
        }

    def select_options(self, chain) -> list[OptionData]:
        options: list[OptionData] = []
        atm = float(getattr(chain, "atm_price", 0) or 0)
        if not atm and chain.underlying:
            atm = float(chain.underlying.mid_price or 0)

        ranked: list[tuple[float, OptionData]] = []
        for option in chain.options.values():
            if not option.underlying:
                continue
            unit_delta = self.unit_delta(option)
            if abs(unit_delta) < self.cfg.min_unit_delta or abs(unit_delta) > self.cfg.max_unit_delta:
                continue
            distance = abs(option.strike_price - atm) if atm else 0
            ranked.append((distance, option))

        ranked.sort(key=lambda item: item[0])
        if self.cfg.atm_strikes > 0 and atm:
            keep = set()
            try:
                indexes = sorted(chain.indexes, key=float)
            except ValueError:
                indexes = list(chain.indexes)
            atm_index = getattr(chain, "atm_index", "") or (indexes[len(indexes) // 2] if indexes else "")
            if atm_index in indexes:
                center = indexes.index(atm_index)
            else:
                center = min(range(len(indexes)), key=lambda i: abs(float(indexes[i]) - atm)) if indexes else 0
            lo = max(0, center - self.cfg.atm_strikes)
            hi = min(len(indexes), center + self.cfg.atm_strikes + 1)
            keep = set(indexes[lo:hi])
            ranked = [item for item in ranked if item[1].chain_index in keep]

        for _distance, option in ranked:
            options.append(option)
        return options

    @staticmethod
    def unit_delta(option: OptionData) -> float:
        size = float(option.size or 1)
        return float(option.theo_delta or 0) / size

    def check_portfolio_risk(self, portfolio: PortfolioData) -> tuple[bool, str]:
        cfg = self.cfg
        if cfg.max_book_lots and abs(portfolio.net_pos) >= cfg.max_book_lots:
            return True, f"组合净仓超限 {portfolio.net_pos}"
        if cfg.max_portfolio_delta and abs(portfolio.pos_delta) >= cfg.max_portfolio_delta:
            return True, f"组合Delta超限 {portfolio.pos_delta:.1f}"
        if cfg.max_portfolio_gamma and abs(portfolio.pos_gamma) >= cfg.max_portfolio_gamma:
            return True, f"组合Gamma超限 {portfolio.pos_gamma:.4f}"
        if cfg.max_portfolio_vega and abs(portfolio.pos_vega) >= cfg.max_portfolio_vega:
            return True, f"组合Vega超限 {portfolio.pos_vega:.1f}"
        return False, ""

    def market_mid(self, tick: TickData | None) -> float:
        if not tick:
            return 0.0
        bid = float(tick.bid_price_1 or 0)
        ask = float(tick.ask_price_1 or 0)
        if bid and ask:
            return (bid + ask) / 2
        return float(tick.last_price or 0)

    def theo_mid(self, option: OptionData) -> float:
        theo = 0.0
        try:
            if option.pricing_impv:
                theo = float(option.calculate_ref_price() or 0)
        except Exception:
            theo = 0.0
        market = self.market_mid(option.tick)
        if theo and market:
            return self.cfg.theo_weight * theo + (1.0 - self.cfg.theo_weight) * market
        return theo or market

    def as_quotes(self, mid: float, inventory: float) -> tuple[float, float, float]:
        """返回 reservation, half_spread（价格单位）。inventory 为相对 max_pos 的 [-1,1]。"""
        gamma = max(self.cfg.gamma, 1e-6)
        kappa = max(self.cfg.kappa, 1e-6)
        sigma = max(self.cfg.sigma, 1e-4)
        tau = max(self.cfg.tau_days, 1 / 365) / 365.0
        q = max(-1.0, min(1.0, inventory))
        reservation = mid - q * gamma * (sigma ** 2) * tau * mid
        half = 0.5 * gamma * (sigma ** 2) * tau * mid + (1.0 / gamma) * math.log(1.0 + gamma / kappa) * mid * 0.02
        return reservation, max(half, 0.0), (sigma ** 2) * tau

    def build_plan(self, option: OptionData, chain) -> QuotePlan:
        cfg = self.cfg
        tick = option.tick
        base = QuotePlan(
            vt_symbol=option.vt_symbol,
            strike=float(option.strike_price or 0),
            option_type="Call" if option.option_type == 1 else "Put",
            net_pos=int(option.net_pos or 0),
            unit_delta=round(self.unit_delta(option), 4),
            iv=round(float(option.mid_impv or 0) * 100, 2),
        )
        if not tick:
            base.reason = "无Tick"
            base.action = "跳过"
            return base
        if self.tick_stale(tick):
            self.cancel_symbol(option.vt_symbol)
            base.reason = "行情过期"
            base.action = "撤单"
            return base

        pricetick = float(option.pricetick or 0.2)
        theo = 0.0
        try:
            if option.pricing_impv:
                theo = float(option.calculate_ref_price() or 0)
        except Exception:
            theo = 0.0
        market = self.market_mid(tick)
        mid = self.theo_mid(option)
        bid_mkt = float(tick.bid_price_1 or 0)
        ask_mkt = float(tick.ask_price_1 or 0)
        last_price = float(tick.last_price or 0)
        base.theo = round(theo, 4)
        base.mid = round(mid, 4)
        base.market_bid = bid_mkt
        base.market_ask = ask_mkt
        base.last_price = last_price
        if mid <= 0:
            base.reason = "无法计算公允价"
            base.action = "跳过"
            return base

        net_pos = base.net_pos
        inv = net_pos / max(cfg.max_pos, 1)
        reservation, as_half, _ = self.as_quotes(mid, inv)

        vega = abs(float(option.theo_vega or 0))
        size = max(float(option.size or 1), 1)
        vega_half = cfg.vol_spread * vega / size / 2

        atm_gamma = 0.0
        atm_opt = chain.calls.get(chain.atm_index) if getattr(chain, "atm_index", "") else None
        if atm_opt:
            atm_gamma = abs(float(atm_opt.theo_gamma or 0))
        rel_gamma = abs(float(option.theo_gamma or 0)) / atm_gamma if atm_gamma else 1.0
        gamma_half = cfg.gamma_spread_ticks * pricetick * rel_gamma / 2

        unit_delta = abs(base.unit_delta)
        otm_half = cfg.otm_spread_ticks * pricetick * abs(unit_delta - 0.5) / 0.5
        inv_half = cfg.inventory_spread_ticks * pricetick * abs(inv)
        min_half = cfg.min_spread_ticks * pricetick / 2

        parts = {
            "AS": as_half,
            "Vega": vega_half,
            "Gamma": gamma_half,
            "虚值": otm_half,
            "库存": inv_half,
            "最小": min_half,
        }
        driver = max(parts, key=parts.get)
        half = min(max(parts.values()), cfg.max_spread_ticks * pricetick / 2)

        bid = floor_to(reservation - half, pricetick)
        ask = ceil_to(reservation + half, pricetick)
        if ask <= bid:
            ask = bid + pricetick

        if ask_mkt:
            bid = min(bid, ask_mkt - pricetick)
        if bid_mkt:
            ask = max(ask, bid_mkt + pricetick)
        if tick.limit_up:
            ask = min(ask, float(tick.limit_up))
            bid = min(bid, float(tick.limit_up) - pricetick)
        if tick.limit_down:
            bid = max(bid, float(tick.limit_down))
            ask = max(ask, float(tick.limit_down) + pricetick)

        base.reservation = round_to(reservation, pricetick)
        base.spread_as = round(as_half * 2, 4)
        base.spread_vega = round(vega_half * 2, 4)
        base.spread_gamma = round(gamma_half * 2, 4)
        base.spread_otm = round(otm_half * 2, 4)
        base.spread_inv = round(inv_half * 2, 4)
        base.spread_driver = driver

        if bid <= 0 or ask <= bid:
            base.reason = "价格不合法/涨跌停"
            base.action = "跳过"
            return base

        bid_vol = self.clip_volume(cfg.quote_volume * max(0.25, 1.0 - cfg.size_skew * inv), option, tick, True)
        ask_vol = self.clip_volume(cfg.quote_volume * max(0.25, 1.0 + cfg.size_skew * inv), option, tick, False)

        reasons: list[str] = []
        allow_bid = net_pos < cfg.max_pos
        allow_ask = net_pos > -cfg.max_pos
        if not allow_bid:
            reasons.append("多仓已满")
        if not allow_ask:
            reasons.append("空仓已满")
        flatten = abs(inv) >= cfg.flatten_inventory
        if flatten and net_pos > 0:
            allow_bid = False
            reasons.append("库存偏多只卖")
        if flatten and net_pos < 0:
            allow_ask = False
            reasons.append("库存偏空只买")
        if self.halted:
            allow_bid = net_pos < 0
            allow_ask = net_pos > 0
            reasons.append(self.halt_reason or "组合熔断")
            if not allow_bid and not allow_ask:
                self.cancel_symbol(option.vt_symbol)
                base.reason = "；".join(reasons) or "熔断无仓可平"
                base.action = "撤单"
                return base

        now = time()
        state = self.working.get(option.vt_symbol)
        if state and now - state.last_fill < cfg.fill_cooldown:
            allow_bid = allow_ask = False
            reasons.append("成交冷却")

        allow_bid = allow_bid and bid_vol > 0
        allow_ask = allow_ask and ask_vol > 0
        if bid_vol <= 0:
            reasons.append("买量裁为0")
        if ask_vol <= 0:
            reasons.append("卖量裁为0")

        if allow_bid and allow_ask:
            action = "双边报价"
        elif allow_bid:
            action = "只买"
        elif allow_ask:
            action = "只卖"
        else:
            action = "观望"

        if not reasons:
            reasons.append(f"价差由{driver}决定")

        base.bid = bid
        base.ask = ask
        base.bid_volume = bid_vol
        base.ask_volume = ask_vol
        base.spread = round_to(ask - bid, pricetick)
        base.allow_bid = allow_bid
        base.allow_ask = allow_ask
        base.quoting = allow_bid or allow_ask
        base.action = action
        base.reason = "；".join(reasons)
        return base

    def clip_volume(self, raw: float, option: OptionData, tick: TickData, is_bid: bool) -> int:
        contract = self.engine.get_contract(option.vt_symbol)
        min_vol = int(getattr(contract, "min_volume", 1) or self.cfg.min_volume)
        volume = int(max(min_vol, round(raw)))
        volume = min(volume, self.cfg.max_order_size)
        book = int(tick.bid_volume_1 if is_bid else tick.ask_volume_1) or 0
        if book:
            volume = min(volume, max(min_vol, book))
        remaining = self.cfg.max_pos - option.net_pos if is_bid else self.cfg.max_pos + option.net_pos
        volume = min(volume, max(0, int(remaining)))
        return volume

    def tick_stale(self, tick: TickData) -> bool:
        dt = getattr(tick, "datetime", None)
        if not isinstance(dt, datetime):
            return False
        now = datetime.now(tz=dt.tzinfo) if dt.tzinfo else datetime.now()
        try:
            age = (now - dt).total_seconds()
        except Exception:
            return False
        return age > self.cfg.stale_tick_sec

    def sync_orders(self, plan: QuotePlan) -> None:
        state = self.working.setdefault(plan.vt_symbol, WorkingQuote())
        option = self._option(plan.vt_symbol)
        if option:
            if option.net_pos != state.last_pos:
                if state.last_pos != 0 or option.net_pos != 0:
                    state.last_fill = time()
                state.last_pos = int(option.net_pos or 0)
        self.refresh_working(state)

        now = time()
        can_replace = now - state.last_replace >= self.cfg.min_quote_life
        pricetick = float(option.pricetick if option else 0.2)

        if not plan.allow_bid:
            self.cancel_side(state, True)
        elif self.need_replace(state.bid_orderid, state.bid_price, plan.bid, pricetick):
            if can_replace or not state.bid_orderid:
                self.cancel_side(state, True)
                self.place(plan, state, True)

        if not plan.allow_ask:
            self.cancel_side(state, False)
        elif self.need_replace(state.ask_orderid, state.ask_price, plan.ask, pricetick):
            if can_replace or not state.ask_orderid:
                self.cancel_side(state, False)
                self.place(plan, state, False)

    def need_replace(self, orderid: str, old_price: float, new_price: float, pricetick: float) -> bool:
        if not orderid:
            return True
        return abs(new_price - old_price) >= pricetick * self.cfg.requote_ticks - 1e-12

    def place(self, plan: QuotePlan, state: WorkingQuote, is_bid: bool) -> None:
        option = self._option(plan.vt_symbol)
        if not option:
            return
        price = plan.bid if is_bid else plan.ask
        volume = plan.bid_volume if is_bid else plan.ask_volume
        if volume <= 0:
            return
        if self.cfg.dry_run:
            self.write(
                f"{'BUY' if is_bid else 'SELL'} {plan.vt_symbol} {volume}@{price} "
                f"mid={plan.mid:.4f} r={plan.reservation} spr={plan.spread} pos={plan.net_pos} [DRY]"
            )
            self.note(f"{plan.action} {plan.vt_symbol} {volume}@{price} {plan.reason}")
            if is_bid:
                state.bid_orderid = "DRY-BID"
                state.bid_price = price
            else:
                state.ask_orderid = "DRY-ASK"
                state.ask_price = price
            state.last_replace = time()
            return

        vt_orderid = self.send_open_or_close(option, is_bid, price, volume)
        if not vt_orderid:
            return
        if is_bid:
            state.bid_orderid = vt_orderid
            state.bid_price = price
        else:
            state.ask_orderid = vt_orderid
            state.ask_price = price
        state.last_replace = time()
        self.write(f"{'BUY' if is_bid else 'SELL'} {plan.vt_symbol} {volume}@{price} [{vt_orderid}]")
        self.note(f"{plan.action} {plan.vt_symbol} {volume}@{price} {plan.reason}")

    def send_open_or_close(self, option: OptionData, is_bid: bool, price: float, volume: int) -> str:
        """优先平仓，剩余再开仓，适配国内期权开平。"""
        last_id = ""
        if is_bid:
            close_vol = min(int(option.short_pos or 0), volume)
            open_vol = volume - close_vol
            if close_vol:
                last_id = self.engine.cover(option.vt_symbol, price, close_vol) or last_id
            if open_vol:
                last_id = self.engine.buy(option.vt_symbol, price, open_vol) or last_id
        else:
            close_vol = min(int(option.long_pos or 0), volume)
            open_vol = volume - close_vol
            if close_vol:
                last_id = self.engine.sell(option.vt_symbol, price, close_vol) or last_id
            if open_vol:
                last_id = self.engine.short(option.vt_symbol, price, open_vol) or last_id
        return last_id

    def refresh_working(self, state: WorkingQuote) -> None:
        for attr in ("bid_orderid", "ask_orderid"):
            orderid = getattr(state, attr)
            if not orderid or str(orderid).startswith("DRY"):
                continue
            order: OrderData | None = self.engine.get_order(orderid)
            if not order or not order.is_active():
                setattr(state, attr, "")

    def cancel_side(self, state: WorkingQuote, is_bid: bool) -> None:
        orderid = state.bid_orderid if is_bid else state.ask_orderid
        if not orderid:
            return
        if not str(orderid).startswith("DRY"):
            self.engine.cancel_order(orderid)
        if is_bid:
            state.bid_orderid = ""
            state.bid_price = 0.0
        else:
            state.ask_orderid = ""
            state.ask_price = 0.0

    def cancel_symbol(self, vt_symbol: str) -> None:
        state = self.working.get(vt_symbol)
        if not state:
            return
        self.cancel_side(state, True)
        self.cancel_side(state, False)
        self.working.pop(vt_symbol, None)

    def cancel_all(self) -> None:
        for vt_symbol in list(self.working):
            self.cancel_symbol(vt_symbol)

    def _option(self, vt_symbol: str) -> OptionData | None:
        opt = self.option_engine()
        if not opt:
            return None
        instrument = opt.instruments.get(vt_symbol)
        return instrument if isinstance(instrument, OptionData) else None


def run(engine: ScriptEngine) -> None:
    AsOptionMaker(engine, CFG).run()
