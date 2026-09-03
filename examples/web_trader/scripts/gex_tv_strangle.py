"""
GEX 宽跨收时间价值（ScriptTrader）

规则：
1. 底层期货，交易对应期权（默认 SA.CZCE 纯碱）
2. IV Rank 高时，在 GEX 墙外卖 14–25Δ 铁鹰（3 档长腿，净权利金至少为翼宽的 25%）
   目标约 45 DTE，21 DTE 移仓；收回 75% 权利金止盈。亏损由翼宽锁定
3. 期货默认可不对冲；铁鹰本身接近 Delta 中性
4. 仓位按单笔最大亏损占净值（SA 默认 6%；IF 上用过的 46% 杠杆在纯碱上会爆仓）
5. 在限定风险内最大化净 theta / 最大亏损

默认组合 SA.CZCE。连接 CTP，期权页初始化组合后在脚本页启动。默认 DRY_RUN=True。
"""

from __future__ import annotations

import json
import math
import os
import traceback
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from time import sleep, time as now_ts

from vnpy.trader.constant import Direction, Offset, OptionType, Status
from vnpy.trader.object import TickData
from vnpy.trader.utility import load_json, round_to, save_json
from vnpy_optionmaster.base import APP_NAME, OptionData, PortfolioData
from vnpy_optionmaster.engine import OptionEngine
from vnpy_optionmaster.pricing.black_76 import (
    calculate_delta,
    calculate_gamma,
    calculate_price,
    calculate_theta,
)
from vnpy_scripttrader import ScriptEngine

STATUS_FILE = "gex_tv_strangle_status.json"
RATE = 0.02


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_d2(spot: float, strike: float, t: float, sigma: float) -> float:
    t = max(float(t), 1.0 / 365.0)
    sigma = max(float(sigma), 1e-4)
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * t) / (sigma * math.sqrt(t))
    return d1 - sigma * math.sqrt(t)


def seller_win_prob(spot: float, strike: float, t: float, sigma: float, cp: int) -> float:
    """卖方胜率：Call 为 P(S_T < K)=N(-d2)，Put 为 P(S_T > K)=N(d2)。"""
    d2 = black_d2(spot, strike, t, sigma)
    if cp > 0:
        return float(norm_cdf(-d2))
    return float(norm_cdf(d2))


@dataclass
class KellyBudget:
    p_call: float
    p_put: float
    p_leg: float
    f_raw: float
    f: float
    budget: float


def range_hold_prob(spot: float, k_put: float, k_call: float, t: float, sigma: float) -> float:
    """风险中性 P(K_put < S_T < K_call) = N(-d2_call) − N(-d2_put)。"""
    if k_call <= k_put:
        return 0.0
    p_below_call = seller_win_prob(spot, k_call, t, sigma, 1)
    p_below_put = seller_win_prob(spot, k_put, t, sigma, 1)
    return float(min(1.0, max(0.0, p_below_call - p_below_put)))


def kelly_margin_budget(
    p_call: float,
    p_put: float,
    nav: float,
    payoff_ratio: float = 1.0,
    scale: float = 0.25,
    cap: float = 0.10,
    p_joint: float | None = None,
) -> KellyBudget:
    """腿胜率或区间存活概率 → 凯利占用 → 保证金预算。

    铁鹰用 p_joint=P(两短腿都虚值到期)、b=净权利金/最大亏损。
    """
    p_call = min(max(float(p_call), 0.0), 1.0)
    p_put = min(max(float(p_put), 0.0), 1.0)
    p = min(max(float(p_joint), 0.0), 1.0) if p_joint is not None else 0.5 * (p_call + p_put)
    b = max(float(payoff_ratio), 1e-6)
    f_raw = (b * p - (1.0 - p)) / b
    f = max(0.0, min(float(cap), f_raw * float(scale)))
    return KellyBudget(
        p_call=p_call,
        p_put=p_put,
        p_leg=p,
        f_raw=f_raw,
        f=f,
        budget=f * max(float(nav), 0.0),
    )


def lsp_value(highs: list[float], lows: list[float], close: float) -> float:
    """价格区间位置：(C-LLV)/(HHV-LLV)。0 靠近区间下沿（偏多），1 靠近上沿（偏空）。"""
    if not highs or not lows:
        return 0.5
    hh = max(highs)
    ll = min(lows)
    if hh <= ll:
        return 0.5
    return min(1.0, max(0.0, (close - ll) / (hh - ll)))


def third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    first_friday = 1 + (4 - first.weekday()) % 7
    return date(year, month, first_friday + 14)


def weekday_on_or_before(day: date) -> date:
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def czce_option_expiry(delivery_year: int, delivery_month: int) -> date:
    """郑商所：交割月前一个月15日（含）之前的倒数第3个交易日。节假日按周末近似。"""
    if delivery_month == 1:
        year, month = delivery_year - 1, 12
    else:
        year, month = delivery_year, delivery_month - 1
    limit = weekday_on_or_before(date(year, month, 15))
    day = limit
    for _ in range(2):
        day -= timedelta(days=1)
        day = weekday_on_or_before(day)
    return day


def list_expiries(today: date, calendar: str = "cffex", count: int = 8) -> list[date]:
    y, m = today.year, today.month
    found: list[date] = []
    for _ in range(18):
        exp = czce_option_expiry(y, m) if calendar == "czce" else third_friday(y, m)
        if exp > today:
            found.append(exp)
            if len(found) >= count:
                break
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return found


def next_month_expiry(today: date, calendar: str = "cffex") -> date:
    """次月到期日。cffex=第三周五；czce=郑商所商品期权规则。"""
    found = list_expiries(today, calendar, 2)
    if len(found) >= 2:
        return found[1]
    if found:
        return found[-1]
    return czce_option_expiry(today.year, today.month) if calendar == "czce" else third_friday(today.year, today.month)


def select_expiry(
    today: date,
    calendar: str = "cffex",
    target_dte: int = 45,
    min_dte: int = 28,
    max_dte: int = 65,
) -> date | None:
    """选最接近目标 DTE 的合约月（默认 ~45 天，落在 28–65）。"""
    found = select_expiries(today, calendar, target_dte, min_dte, max_dte, exclude=(), count=1)
    return found[0] if found else None


def select_expiries(
    today: date,
    calendar: str = "cffex",
    target_dte: int = 45,
    min_dte: int = 28,
    max_dte: int = 65,
    exclude: tuple[date, ...] | set[date] = (),
    count: int = 1,
) -> list[date]:
    """按距目标 DTE 近到远，选出未持有的到期日。"""
    blocked = set(exclude)
    cands = [
        exp
        for exp in list_expiries(today, calendar, 8)
        if min_dte <= (exp - today).days <= max_dte and exp not in blocked
    ]
    cands.sort(key=lambda exp: abs((exp - today).days - target_dte))
    return cands[: max(int(count), 0)]


def sa_strike_step(spot: float) -> float:
    if spot <= 1000:
        return 10.0
    if spot <= 2000:
        return 20.0
    return 40.0


def io_strike_step(spot: float) -> float:
    """中金所股指期权近月行权价间距（IO/HO/MO 相同）。"""
    if spot < 2500:
        return 25.0
    if spot < 5000:
        return 50.0
    return 100.0


def is_cffex_index_option(name: str) -> bool:
    token = (name or "").upper()
    return any(tag in token for tag in ("IO.", "HO.", "MO.", "IF.", "IH.", "IM."))


def strike_step_for(portfolio_name: str, spot: float, fallback: float) -> float:
    if is_cffex_index_option(portfolio_name):
        return io_strike_step(spot)
    if "SA" in (portfolio_name or "").upper():
        return sa_strike_step(spot)
    return fallback


def env_truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def cffex_session(now: datetime | None = None) -> bool:
    """中金所股指期权：工作日 09:30–11:30、13:00–15:00。提前 5 分钟允许询价。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (dtime(9, 25) <= t <= dtime(11, 31)) or (dtime(12, 58) <= t <= dtime(15, 1))


def dte_years(expiry: date, today: date) -> float:
    return max((expiry - today).days, 1) / 365.0


def round_strike(spot: float, step: float) -> float:
    return max(step, round(spot / step) * step)


def option_margin(spot: float, strike: float, premium: float, size: float, rate: float, min_rate: float) -> float:
    otm = max(0.0, abs(spot - strike))
    return size * (max(premium, 0.0) + max(rate * spot - otm, min_rate * spot))


def strangle_margin(spot: float, k_call: float, k_put: float, p_call: float, p_put: float, size: float, rate: float, min_rate: float) -> float:
    m_c = option_margin(spot, k_call, p_call, size, rate, min_rate)
    m_p = option_margin(spot, k_put, p_put, size, rate, min_rate)
    return max(m_c, m_p) + 0.5 * min(m_c, m_p)


def iv_rank(current: float, history: list[float]) -> float:
    if not history:
        return 50.0
    below = sum(1 for item in history if item <= current)
    return 100.0 * below / len(history)


def synthetic_gex_walls(spot: float, sigma: float, t: float, step: float, size: float) -> tuple[float, float]:
    """无持仓量时用对数正态 OI 代理，计算 Call/Put GEX 墙。"""
    atm = round_strike(spot, step)
    best_call_gex = -1e18
    best_put_gex = 1e18
    call_wall = atm + step
    put_wall = atm - step
    width = max(4 * step, round_strike(spot * sigma * math.sqrt(max(t, 1 / 365)) * 2.5, step))
    k = atm - width
    while k <= atm + width:
        if k <= 0:
            k += step
            continue
        gamma = max(calculate_gamma(spot, k, RATE, t, max(sigma, 0.05)), 0.0)
        dist = (k - spot) / max(spot * 0.08, step)
        oi_call = math.exp(-0.5 * dist * dist) * max(spot / k, 0.3)
        oi_put = math.exp(-0.5 * ((k - spot) / max(spot * 0.10, step)) ** 2) * max(k / spot, 0.3)
        call_gex = gamma * oi_call * spot * 0.01 * size
        put_gex = -gamma * oi_put * spot * 0.01 * size
        if call_gex > best_call_gex:
            best_call_gex = call_gex
            call_wall = k
        if put_gex < best_put_gex:
            best_put_gex = put_gex
            put_wall = k
        k += step
    if put_wall >= call_wall:
        put_wall = atm - step
        call_wall = atm + step
    return float(call_wall), float(put_wall)


def live_gex_walls(chain, spot: float, step: float) -> tuple[float, float] | None:
    best_call_gex = -1e18
    best_put_gex = 1e18
    call_wall = None
    put_wall = None
    for index in getattr(chain, "indexes", []) or []:
        call = chain.calls.get(index) if getattr(chain, "calls", None) else None
        put = chain.puts.get(index) if getattr(chain, "puts", None) else None
        option = call or put
        if not option:
            continue
        try:
            strike = float(option.strike_price or index)
        except (TypeError, ValueError):
            continue
        call_oi = float(getattr(getattr(call, "tick", None), "open_interest", 0) or 0) if call else 0.0
        put_oi = float(getattr(getattr(put, "tick", None), "open_interest", 0) or 0) if put else 0.0
        call_gex = float(getattr(call, "theo_gamma", 0) or 0) * call_oi * spot * 0.01 if call else 0.0
        put_gex = -float(getattr(put, "theo_gamma", 0) or 0) * put_oi * spot * 0.01 if put else 0.0
        if call_gex > best_call_gex:
            best_call_gex, call_wall = call_gex, strike
        if put_gex < best_put_gex:
            best_put_gex, put_wall = put_gex, strike
    if call_wall is None or put_wall is None:
        return None
    if put_wall >= call_wall:
        atm = round_strike(spot, step)
        return atm + step, atm - step
    return float(call_wall), float(put_wall)


@dataclass
class StrikePick:
    k_call: float
    k_put: float
    p_call: float
    p_put: float
    theta: float
    margin: float
    efficiency: float
    win_prob: float
    p_call_win: float
    p_put_win: float
    delta: float
    call_wall: float
    put_wall: float
    k_call_long: float = 0.0
    k_put_long: float = 0.0
    p_call_long: float = 0.0
    p_put_long: float = 0.0
    credit: float = 0.0
    width: float = 0.0
    max_loss: float = 0.0
    payoff_ratio: float = 1.0
    range_prob: float = 0.0
    d_call: float = 0.0
    d_put: float = 0.0


def pick_strangle(
    spot: float,
    sigma: float,
    t: float,
    step: float,
    size: float,
    call_wall: float,
    put_wall: float,
    min_delta: float = 0.08,
    max_delta: float = 0.28,
    margin_rate: float = 0.12,
    min_margin_rate: float = 0.07,
    price_floor: float = 0.2,
) -> StrikePick | None:
    """在 GEX 墙外、Delta 带宽内，选 theta/保证金最大的宽跨。

    max_loss 用交易所风格卖方保证金作风险单元（裸卖无有界最大亏损）。
    """
    atm = round_strike(spot, step)
    sigma = max(sigma, 0.05)
    t = max(t, 1.0 / 365.0)
    best: StrikePick | None = None
    k = atm - 16 * step
    calls: list[tuple[float, float, float, float]] = []
    puts: list[tuple[float, float, float, float]] = []
    while k <= atm + 16 * step:
        if k > 0:
            for cp, bucket in ((1, calls), (-1, puts)):
                delta = abs(calculate_delta(spot, k, RATE, t, sigma, cp))
                if not (min_delta <= delta <= max_delta):
                    continue
                price = max(calculate_price(spot, k, RATE, t, sigma, cp), price_floor)
                theta = -calculate_theta(spot, k, RATE, t, sigma, cp)
                bucket.append((k, price, theta, delta if cp > 0 else -delta))
        k += step
    calls = [row for row in calls if row[0] >= max(call_wall, atm + step) - 1e-9]
    puts = [row for row in puts if row[0] <= min(put_wall, atm - step) + 1e-9]
    for k_c, p_c, th_c, d_c in calls:
        for k_p, p_p, th_p, d_p in puts:
            if k_c - k_p < 2 * step:
                continue
            margin = strangle_margin(spot, k_c, k_p, p_c, p_p, size, margin_rate, min_margin_rate)
            if margin <= 0:
                continue
            credit = p_c + p_p
            if credit <= price_floor:
                continue
            theta = th_c + th_p
            efficiency = theta / margin
            p_call = seller_win_prob(spot, k_c, t, sigma, 1)
            p_put = seller_win_prob(spot, k_p, t, sigma, -1)
            p_range = range_hold_prob(spot, k_p, k_c, t, sigma)
            credit_cash = credit * size
            cand = StrikePick(
                k_call=k_c,
                k_put=k_p,
                p_call=p_c,
                p_put=p_p,
                theta=theta,
                margin=margin,
                efficiency=efficiency,
                win_prob=p_range,
                p_call_win=p_call,
                p_put_win=p_put,
                delta=d_c + d_p,
                call_wall=call_wall,
                put_wall=put_wall,
                credit=credit,
                width=0.0,
                max_loss=margin,
                payoff_ratio=credit_cash / max(margin, 1e-6),
                range_prob=p_range,
                d_call=d_c,
                d_put=d_p,
            )
            if best is None or cand.efficiency > best.efficiency:
                best = cand
    return best


def pick_iron_condor(
    spot: float,
    sigma: float,
    t: float,
    step: float,
    size: float,
    call_wall: float,
    put_wall: float,
    min_delta: float = 0.14,
    max_delta: float = 0.25,
    wing_steps: int = 3,
    min_credit_frac: float = 0.25,
    price_floor: float = 0.2,
) -> StrikePick | None:
    """GEX 墙外 14–25Δ 短腿 + 外侧长腿，只保留净权利金/翼宽足够厚的铁鹰。"""
    atm = round_strike(spot, step)
    sigma = max(sigma, 0.05)
    t = max(t, 1.0 / 365.0)
    wing = max(int(wing_steps), 1) * step
    calls: list[tuple[float, float, float, float]] = []
    puts: list[tuple[float, float, float, float]] = []
    k = atm - 18 * step
    while k <= atm + 18 * step:
        if k > 0:
            for cp, bucket in ((1, calls), (-1, puts)):
                delta = abs(calculate_delta(spot, k, RATE, t, sigma, cp))
                if not (min_delta <= delta <= max_delta):
                    continue
                price = max(calculate_price(spot, k, RATE, t, sigma, cp), price_floor)
                theta = -calculate_theta(spot, k, RATE, t, sigma, cp)
                bucket.append((k, price, theta, delta if cp > 0 else -delta))
        k += step
    calls = [row for row in calls if row[0] >= max(call_wall, atm + step) - 1e-9]
    puts = [row for row in puts if row[0] <= min(put_wall, atm - step) + 1e-9]
    best: StrikePick | None = None
    for k_c, p_c, th_c, d_c in calls:
        k_lc = k_c + wing
        p_lc = max(calculate_price(spot, k_lc, RATE, t, sigma, 1), price_floor)
        th_lc = -calculate_theta(spot, k_lc, RATE, t, sigma, 1)
        d_lc = calculate_delta(spot, k_lc, RATE, t, sigma, 1)
        for k_p, p_p, th_p, d_p in puts:
            if k_c - k_p < 2 * step:
                continue
            k_lp = k_p - wing
            if k_lp <= 0:
                continue
            p_lp = max(calculate_price(spot, k_lp, RATE, t, sigma, -1), price_floor)
            th_lp = -calculate_theta(spot, k_lp, RATE, t, sigma, -1)
            d_lp = calculate_delta(spot, k_lp, RATE, t, sigma, -1)
            credit = (p_c + p_p) - (p_lc + p_lp)
            width = wing
            if credit <= price_floor or credit / width < min_credit_frac:
                continue
            max_loss = size * (width - credit)
            if max_loss <= 0:
                continue
            theta = (th_c + th_p) - (th_lc + th_lp)
            p_call = seller_win_prob(spot, k_c, t, sigma, 1)
            p_put = seller_win_prob(spot, k_p, t, sigma, -1)
            p_range = range_hold_prob(spot, k_p, k_c, t, sigma)
            cand = StrikePick(
                k_call=k_c,
                k_put=k_p,
                p_call=p_c,
                p_put=p_p,
                theta=theta,
                margin=max_loss,
                efficiency=theta / max_loss,
                win_prob=p_range,
                p_call_win=p_call,
                p_put_win=p_put,
                delta=(-d_c - d_p + d_lc + d_lp),
                call_wall=call_wall,
                put_wall=put_wall,
                k_call_long=k_lc,
                k_put_long=k_lp,
                p_call_long=p_lc,
                p_put_long=p_lp,
                credit=credit,
                width=width,
                max_loss=max_loss,
                payoff_ratio=credit / max(width - credit, 1e-6),
                range_prob=p_range,
                d_call=d_c,
                d_put=d_p,
            )
            if best is None or cand.efficiency > best.efficiency:
                best = cand
    return best


@dataclass
class Config:
    portfolio_name: str = "SA.CZCE"
    hedge_symbol: str = ""
    dry_run: bool = True
    loop_interval: float = 2.0
    capital: float = 1_000_000.0
    strike_step: float = 20.0
    option_size: float = 20.0
    futures_size: float = 20.0
    margin_rate: float = 0.10
    min_margin_rate: float = 0.05
    roll_dte: int = 21
    target_dte: int = 45
    min_entry_dte: int = 28
    max_entry_dte: int = 65
    lsp_lookback: int = 20
    hv_lookback: int = 20
    iv_rank_lookback: int = 60
    iv_rank_min: float = 40.0
    min_delta: float = 0.14
    max_delta: float = 0.25
    wing_steps: int = 3
    min_credit_frac: float = 0.25
    take_profit: float = 0.25
    delta_stop: float = 0.99
    kelly_scale: float = 0.25
    kelly_cap: float = 0.10
    payoff_ratio: float = 1.0
    risk_cap: float = 0.06
    lsp_lo: float = 0.0
    lsp_hi: float = 1.0
    hv_expand: float = 9.0
    max_lots: int = 80
    hedge: bool = False
    price_floor: float = 0.2
    capital_share: float = 1.0


def condor_lots(pick: StrikePick, nav: float, scale: float, cap: float, risk_cap: float, max_lots: int) -> tuple[int, KellyBudget]:
    """仓位先按单笔最大亏损占净值，再用凯利封顶。RN 凯利为负时仍按风险预算开仓（波动率溢价）。"""
    kb = kelly_margin_budget(
        pick.p_call_win,
        pick.p_put_win,
        nav,
        pick.payoff_ratio or 1.0,
        scale,
        cap,
        pick.range_prob or pick.win_prob,
    )
    risk_lots = int((float(risk_cap) * max(float(nav), 0.0)) // max(pick.max_loss or pick.margin, 1.0))
    kelly_lots = int(kb.budget // max(pick.margin, 1.0)) if kb.f > 0 else risk_lots
    lots = min(int(max_lots), risk_lots, kelly_lots)
    return max(lots, 0), kb


CFG = Config()


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


def live_config(portfolio_name: str, dry_run: bool, capital_share: float) -> Config:
    """Build live Config.

    CFFEX IO 默认：翼宽 5 + 权利金/翼宽≥30%（IF 日线全样本与 2024+ 样本外
    Calmar/Sharpe 优于原 3 翼 / 25% 门槛）。不叠加过严 Delta，以免开仓过少。
    risk_cap 仍默认 6%；提高收益可设 LIVE_RISK_CAP=0.10~0.12。
    """
    cffex = is_cffex_index_option(portfolio_name)
    wing_default = 5 if cffex else CFG.wing_steps
    credit_default = 0.30 if cffex else CFG.min_credit_frac
    cfg = Config(
        portfolio_name=portfolio_name,
        dry_run=dry_run,
        option_size=100.0 if cffex else CFG.option_size,
        futures_size=300.0 if cffex else CFG.futures_size,
        strike_step=50.0 if cffex else CFG.strike_step,
        margin_rate=0.12 if cffex else CFG.margin_rate,
        min_margin_rate=0.06 if cffex else CFG.min_margin_rate,
        capital_share=capital_share,
        hedge=False,
        price_floor=0.2 if cffex else CFG.price_floor,
        wing_steps=_env_int("LIVE_WING_STEPS", wing_default),
        min_credit_frac=_env_float("LIVE_MIN_CREDIT_FRAC", credit_default),
        min_delta=_env_float("LIVE_MIN_DELTA", CFG.min_delta),
        max_delta=_env_float("LIVE_MAX_DELTA", CFG.max_delta),
        iv_rank_min=_env_float("LIVE_IV_RANK_MIN", CFG.iv_rank_min),
        take_profit=_env_float("LIVE_TAKE_PROFIT", CFG.take_profit),
        max_lots=_env_int("LIVE_MAX_LOTS", CFG.max_lots),
        risk_cap=_env_float("LIVE_RISK_CAP", CFG.risk_cap),
    )
    if cfg.min_delta >= cfg.max_delta:
        cfg.min_delta, cfg.max_delta = CFG.min_delta, CFG.max_delta
    return cfg


def configs_from_env() -> list[Config]:
    raw = (os.getenv("LIVE_PORTFOLIOS") or "").strip()
    if not raw:
        return []
    names = [item.strip() for item in raw.split(",") if item.strip()]
    if not names:
        return []
    dry_run = env_truthy("LIVE_DRY_RUN", True)
    share = 1.0 / len(names)
    return [live_config(name, dry_run, share) for name in names]


@dataclass
class Book:
    expiry: date | None = None
    k_call: float = 0.0
    k_put: float = 0.0
    k_call_long: float = 0.0
    k_put_long: float = 0.0
    lots: int = 0
    call_symbol: str = ""
    put_symbol: str = ""
    call_long_symbol: str = ""
    put_long_symbol: str = ""
    entry_call: float = 0.0
    entry_put: float = 0.0
    entry_call_long: float = 0.0
    entry_put_long: float = 0.0
    entry_credit: float = 0.0
    fut_lots: float = 0.0
    notes: deque[str] = field(default_factory=lambda: deque(maxlen=40))


class GexTvStrangle:
    def __init__(self, engine: ScriptEngine, cfg: Config, bundle: "LiveBundle | None" = None) -> None:
        self.engine = engine
        self.cfg = cfg
        self.bundle = bundle
        self.book = Book()
        self.day_highs: deque[float] = deque(maxlen=cfg.iv_rank_lookback + 5)
        self.day_lows: deque[float] = deque(maxlen=cfg.iv_rank_lookback + 5)
        self.day_closes: deque[float] = deque(maxlen=cfg.iv_rank_lookback + 5)
        self.hv_hist: deque[float] = deque(maxlen=cfg.iv_rank_lookback)
        self.today: date | None = None
        self.session_high = 0.0
        self.session_low = 0.0
        self.last_spot = 0.0
        self.last_extra: dict = {}
        self.recovered = False
        self.last_skip = ""
        self.last_skip_ts = 0.0
        self.busy = False

    def write(self, msg: str) -> None:
        line = f"[{self.cfg.portfolio_name}] {msg}"
        self.engine.write_log(f"[GEX-TV] {line}")
        stamp = f"{datetime.now().strftime('%H:%M:%S')} {line}"
        self.book.notes.append(stamp)
        if self.bundle is not None:
            self.bundle.notes.append(stamp)

    def option_engine(self) -> OptionEngine | None:
        engine = self.engine.main_engine.get_engine(APP_NAME)
        return engine if isinstance(engine, OptionEngine) else None

    def portfolio(self) -> PortfolioData | None:
        opt = self.option_engine()
        return opt.portfolios.get(self.cfg.portfolio_name) if opt else None

    def book_payload(self) -> dict:
        return {
            "expiry": self.book.expiry.isoformat() if self.book.expiry else "",
            "k_call": self.book.k_call,
            "k_put": self.book.k_put,
            "k_call_long": self.book.k_call_long,
            "k_put_long": self.book.k_put_long,
            "lots": self.book.lots,
            "call_symbol": self.book.call_symbol,
            "put_symbol": self.book.put_symbol,
            "call_long_symbol": self.book.call_long_symbol,
            "put_long_symbol": self.book.put_long_symbol,
            "fut_lots": self.book.fut_lots,
            "entry_credit": self.book.entry_credit,
        }

    def snapshot(self) -> dict:
        payload = {
            "dry_run": self.cfg.dry_run,
            "book": self.book_payload(),
            "hv_hist": list(self.hv_hist),
            "day_closes": list(self.day_closes),
            "day_highs": list(self.day_highs),
            "day_lows": list(self.day_lows),
        }
        payload.update(self.last_extra)
        return payload

    def publish(self, extra: dict | None = None) -> None:
        if extra:
            self.last_extra = extra
        if self.bundle is not None:
            return
        payload = {
            "active": True,
            "updated": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "dry_run": self.cfg.dry_run,
            "params": asdict(self.cfg),
            "portfolio": self.cfg.portfolio_name,
            "book": self.book_payload(),
            "decisions": list(self.book.notes),
        }
        if extra:
            payload.update(extra)
        try:
            save_json(STATUS_FILE, payload)
        except Exception as exc:
            self.write(f"状态写入失败: {exc}")

    def chain_list(self, portfolio: PortfolioData):
        symbols = sorted(set(getattr(portfolio, "_chains", {}) or {}) | set(portfolio.chains or {}))
        items = []
        for symbol in symbols:
            chain = (portfolio.chains.get(symbol) if portfolio.chains else None) or (
                getattr(portfolio, "_chains", {}) or {}
            ).get(symbol)
            if chain:
                items.append((symbol, chain, int(getattr(chain, "days_to_expiry", 0) or 0)))
        items.sort(key=lambda row: row[2])
        return items

    def pick_next_month_chain(self, portfolio: PortfolioData):
        items = self.chain_list(portfolio)
        window = [
            row
            for row in items
            if self.cfg.min_entry_dte <= row[2] <= self.cfg.max_entry_dte
        ]
        if window:
            return min(window, key=lambda row: abs(row[2] - self.cfg.target_dte))
        later = [row for row in items if row[2] > self.cfg.roll_dte]
        return later[0] if later else (None, None, 0)

    def find_option(self, chain, strike: float, want_call: bool) -> OptionData | None:
        bucket = chain.calls if want_call else chain.puts
        best = None
        best_diff = 1e18
        for option in (bucket or {}).values():
            diff = abs(float(option.strike_price) - strike)
            if diff < best_diff:
                best, best_diff = option, diff
        return best

    def mid_price(self, tick: TickData | None, fallback: float = 0.0) -> float:
        if not tick:
            return fallback
        bid = float(getattr(tick, "bid_price_1", 0) or 0)
        ask = float(getattr(tick, "ask_price_1", 0) or 0)
        last = float(getattr(tick, "last_price", 0) or 0)
        if bid > 0 and ask > 0:
            return 0.5 * (bid + ask)
        return last or fallback

    def account_nav(self) -> float:
        try:
            accounts = self.engine.get_all_accounts()
        except Exception:
            accounts = []
        if accounts:
            balance = float(getattr(accounts[0], "balance", 0) or self.cfg.capital)
        else:
            balance = self.cfg.capital
        return max(balance * max(self.cfg.capital_share, 0.0), 0.0)

    def net_pos(self, vt_symbol: str) -> float:
        net = 0.0
        try:
            positions = self.engine.get_all_positions() or []
        except Exception:
            positions = []
        for pos in positions:
            if getattr(pos, "vt_symbol", "") != vt_symbol:
                continue
            volume = float(getattr(pos, "volume", 0) or 0)
            if getattr(pos, "direction", None) == Direction.SHORT:
                net -= volume
            else:
                net += volume
        return net

    def pricetick(self, vt_symbol: str) -> float:
        contract = self.engine.get_contract(vt_symbol)
        tick = float(getattr(contract, "pricetick", 0) or 0) if contract else 0.0
        return tick if tick > 0 else 0.2

    def align_price(self, price: float, vt_symbol: str = "") -> float:
        """Force CTP-legal prices: multiples of contract pricetick (IO/HO/MO = 0.2)."""
        step = self.pricetick(vt_symbol) if vt_symbol else 0.2
        step = max(float(step), 1e-6)
        aligned = round(float(price) / step) * step
        # Avoid float dust like 22.40000000001; IO ticks are 1 decimal.
        decimals = max(0, min(6, len(f"{step:.10f}".rstrip("0").split(".")[-1])))
        return float(round(aligned, decimals))

    def aggressive_price(self, tick: TickData | None, buy: bool, fallback: float, vt_symbol: str = "") -> float:
        symbol = vt_symbol or (getattr(tick, "vt_symbol", "") if tick else "")
        step = self.pricetick(symbol) if symbol else 0.2
        if not tick:
            return self.align_price(fallback, symbol)
        bid = float(getattr(tick, "bid_price_1", 0) or 0)
        ask = float(getattr(tick, "ask_price_1", 0) or 0)
        last = float(getattr(tick, "last_price", 0) or 0)
        if buy:
            raw = ask if ask > 0 else (last + step if last > 0 else fallback)
        else:
            raw = bid if bid > 0 else (last - step if last > 0 else fallback)
        if raw <= 0:
            return self.align_price(fallback, symbol)
        return self.align_price(raw, symbol)

    def cancel_symbol(self, vt_symbol: str) -> None:
        try:
            orders = self.engine.main_engine.get_all_active_orders() or []
        except Exception:
            orders = []
        for order in orders:
            if getattr(order, "vt_symbol", "") != vt_symbol:
                continue
            status = getattr(order, "status", None)
            if status in {Status.ALLTRADED, Status.CANCELLED, Status.REJECTED}:
                continue
            try:
                self.engine.cancel_order(order.vt_orderid)
            except Exception:
                pass

    def wait_net(self, vt_symbol: str, target: float, timeout: float = 8.0) -> bool:
        deadline = now_ts() + timeout
        while now_ts() < deadline and getattr(self.engine, "strategy_active", True):
            if abs(self.net_pos(vt_symbol) - target) < 0.51:
                return True
            sleep(0.4)
        return abs(self.net_pos(vt_symbol) - target) < 0.51

    def restore_state(self) -> None:
        data = load_json(STATUS_FILE) or {}
        books = data.get("books") if isinstance(data, dict) else None
        saved = None
        if isinstance(books, dict):
            saved = books.get(self.cfg.portfolio_name)
        elif data.get("params", {}).get("portfolio_name") == self.cfg.portfolio_name:
            saved = data
        elif data.get("portfolio") == self.cfg.portfolio_name:
            saved = data
        if not isinstance(saved, dict):
            self.seed_hv_from_cache()
            return
        for key, dest in (
            ("hv_hist", self.hv_hist),
            ("day_closes", self.day_closes),
            ("day_highs", self.day_highs),
            ("day_lows", self.day_lows),
        ):
            for value in saved.get(key) or []:
                try:
                    dest.append(float(value))
                except (TypeError, ValueError):
                    continue
        if len(self.day_closes) < self.cfg.hv_lookback + 1:
            self.seed_hv_from_cache()
        elif not self.hv_hist:
            self.rebuild_hv_hist()
        book = saved.get("book") if isinstance(saved.get("book"), dict) else saved
        lots = int(book.get("lots") or 0)
        call_s = str(book.get("call_symbol") or "")
        put_s = str(book.get("put_symbol") or "")
        call_l = str(book.get("call_long_symbol") or "")
        put_l = str(book.get("put_long_symbol") or "")
        if lots > 0 and call_s and put_s:
            short_call = self.net_pos(call_s)
            short_put = self.net_pos(put_s)
            live_lots = int(round(min(abs(short_call), abs(short_put))))
            if live_lots >= 1 and short_call < 0 and short_put < 0:
                self.book.lots = live_lots
                self.book.k_call = float(book.get("k_call") or 0)
                self.book.k_put = float(book.get("k_put") or 0)
                self.book.k_call_long = float(book.get("k_call_long") or 0)
                self.book.k_put_long = float(book.get("k_put_long") or 0)
                self.book.call_symbol = call_s
                self.book.put_symbol = put_s
                self.book.call_long_symbol = call_l
                self.book.put_long_symbol = put_l
                self.book.entry_credit = float(book.get("entry_credit") or 0)
                expiry = book.get("expiry") or ""
                if expiry:
                    try:
                        self.book.expiry = date.fromisoformat(str(expiry)[:10])
                    except ValueError:
                        pass
                self.write(f"恢复持仓 {live_lots} 手 {call_s} / {put_s}")
                self.recovered = True

    def rebuild_hv_hist(self) -> None:
        closes = list(self.day_closes)
        n = self.cfg.hv_lookback
        self.hv_hist.clear()
        if len(closes) < n + 1:
            return
        for end in range(n, len(closes)):
            window = closes[end - n : end + 1]
            rets = [math.log(window[i] / window[i - 1]) for i in range(1, len(window)) if window[i - 1] > 0]
            if len(rets) < 5:
                continue
            mean = sum(rets) / len(rets)
            var = sum((x - mean) ** 2 for x in rets) / max(len(rets) - 1, 1)
            self.hv_hist.append(max(0.08, min(0.80, math.sqrt(var) * math.sqrt(242))))

    def seed_hv_from_cache(self) -> None:
        if len(self.day_closes) >= self.cfg.hv_lookback + 1:
            self.rebuild_hv_hist()
            return
        product = self.cfg.portfolio_name.split(".")[0].upper()
        fname = {
            "IO": "if_daily_cache.json",
            "IF": "if_daily_cache.json",
            "HO": "ih_daily_cache.json",
            "IH": "ih_daily_cache.json",
            "MO": "im_daily_cache.json",
            "IM": "im_daily_cache.json",
        }.get(product)
        if not fname:
            return
        path = Path(__file__).resolve().parent.joinpath(fname)
        if not path.exists():
            return
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        closes: list[float] = []
        for row in rows:
            try:
                if isinstance(row, (list, tuple)) and len(row) >= 5:
                    px = float(row[4])
                elif isinstance(row, dict):
                    px = float(row.get("close") or 0)
                else:
                    continue
            except (TypeError, ValueError):
                continue
            if px > 0:
                closes.append(px)
        if len(closes) < self.cfg.hv_lookback + 1:
            return
        self.day_closes.clear()
        self.day_highs.clear()
        self.day_lows.clear()
        for px in closes[-(self.cfg.iv_rank_lookback + 5) :]:
            self.day_closes.append(px)
            self.day_highs.append(px)
            self.day_lows.append(px)
        self.rebuild_hv_hist()
        self.write(f"已从 {fname} 灌入 HV 样本 {len(self.day_closes)} 日 / {len(self.hv_hist)} 段")

    def skip_once(self, reason: str) -> None:
        now = now_ts()
        if reason == self.last_skip and now - self.last_skip_ts < 60:
            return
        self.last_skip = reason
        self.last_skip_ts = now
        self.write(reason)

    def realized_hv(self, lookback: int | None = None) -> float:
        closes = list(self.day_closes)
        n = lookback or self.cfg.hv_lookback
        if len(closes) < n + 1:
            return 0.18
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - n, len(closes)) if closes[i - 1] > 0]
        if len(rets) < 5:
            return 0.18
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / max(len(rets) - 1, 1)
        return max(0.08, min(0.80, math.sqrt(var) * math.sqrt(242)))

    def update_daily_bars(self, spot: float, today: date) -> None:
        if self.today is None:
            self.today = today
            self.session_high = self.session_low = spot
            return
        if today != self.today:
            if self.last_spot > 0:
                self.day_highs.append(self.session_high)
                self.day_lows.append(self.session_low)
                self.day_closes.append(self.last_spot)
                self.hv_hist.append(self.realized_hv())
            self.today = today
            self.session_high = self.session_low = spot
            return
        self.session_high = max(self.session_high, spot)
        self.session_low = min(self.session_low, spot) if self.session_low else spot

    def send_short(self, vt_symbol: str, price: float, volume: int) -> None:
        if volume <= 0 or price <= 0:
            return
        price = self.align_price(price, vt_symbol)
        if self.cfg.dry_run:
            self.write(f"模拟卖开 {vt_symbol} x{volume} @{price:.2f}")
            return
        self.engine.short(vt_symbol, price, volume)

    def send_cover(self, vt_symbol: str, price: float, volume: int) -> None:
        if volume <= 0 or price <= 0:
            return
        price = self.align_price(price, vt_symbol)
        if self.cfg.dry_run:
            self.write(f"模拟买平 {vt_symbol} x{volume} @{price:.2f}")
            return
        self.engine.cover(vt_symbol, price, volume)

    def send_long(self, vt_symbol: str, price: float, volume: int) -> None:
        if volume <= 0 or price <= 0:
            return
        price = self.align_price(price, vt_symbol)
        if self.cfg.dry_run:
            self.write(f"模拟买开 {vt_symbol} x{volume} @{price:.2f}")
            return
        self.engine.buy(vt_symbol, price, volume)

    def send_sell(self, vt_symbol: str, price: float, volume: int) -> None:
        if volume <= 0 or price <= 0:
            return
        price = self.align_price(price, vt_symbol)
        if self.cfg.dry_run:
            self.write(f"模拟卖平 {vt_symbol} x{volume} @{price:.2f}")
            return
        self.engine.sell(vt_symbol, price, volume)

    def send_hedge(self, vt_symbol: str, target: float, spot: float) -> None:
        delta_lots = target - self.book.fut_lots
        if abs(delta_lots) < 0.5:
            return
        lots = int(round(delta_lots))
        price = round_to(spot, 1.0)
        if self.cfg.dry_run:
            self.write(f"模拟对冲 {vt_symbol} {lots:+d} @{price:.1f}")
            self.book.fut_lots += lots
            return
        if lots > 0:
            self.engine.buy(vt_symbol, price, abs(lots))
        else:
            self.engine.short(vt_symbol, price, abs(lots))
        self.book.fut_lots += lots

    def close_book(
        self,
        call: OptionData | None,
        put: OptionData | None,
        call_long: OptionData | None = None,
        put_long: OptionData | None = None,
    ) -> None:
        if self.book.lots <= 0 or self.busy:
            return
        if not self.cfg.dry_run and not cffex_session() and is_cffex_index_option(self.cfg.portfolio_name):
            self.skip_once("非交易时段，持仓过夜，不平仓")
            return
        self.busy = True
        lots = self.book.lots
        try:
            if call:
                self.send_cover(
                    call.vt_symbol,
                    self.aggressive_price(call.tick, True, self.book.entry_call, call.vt_symbol),
                    lots,
                )
            if put:
                self.send_cover(
                    put.vt_symbol,
                    self.aggressive_price(put.tick, True, self.book.entry_put, put.vt_symbol),
                    lots,
                )
            if not self.cfg.dry_run:
                if call:
                    self.wait_net(call.vt_symbol, 0.0, 8.0)
                    self.cancel_symbol(call.vt_symbol)
                if put:
                    self.wait_net(put.vt_symbol, 0.0, 8.0)
                    self.cancel_symbol(put.vt_symbol)
            if call_long:
                self.send_sell(
                    call_long.vt_symbol,
                    self.aggressive_price(call_long.tick, False, self.book.entry_call_long, call_long.vt_symbol),
                    lots,
                )
            if put_long:
                self.send_sell(
                    put_long.vt_symbol,
                    self.aggressive_price(put_long.tick, False, self.book.entry_put_long, put_long.vt_symbol),
                    lots,
                )
            if not self.cfg.dry_run:
                if call_long:
                    self.wait_net(call_long.vt_symbol, 0.0, 8.0)
                    self.cancel_symbol(call_long.vt_symbol)
                if put_long:
                    self.wait_net(put_long.vt_symbol, 0.0, 8.0)
                    self.cancel_symbol(put_long.vt_symbol)
            self.write(
                f"平仓铁鹰 {lots} 手 {self.book.k_put_long:.0f}/{self.book.k_put:.0f}/"
                f"{self.book.k_call:.0f}/{self.book.k_call_long:.0f}"
            )
            self.book.lots = 0
            self.book.call_symbol = self.book.put_symbol = ""
            self.book.call_long_symbol = self.book.put_long_symbol = ""
            self.book.entry_credit = 0.0
        finally:
            self.busy = False

    def open_book(
        self,
        pick: StrikePick,
        call: OptionData,
        put: OptionData,
        call_long: OptionData,
        put_long: OptionData,
        nav: float,
    ) -> None:
        if self.busy or self.book.lots > 0:
            return
        lots, kb = condor_lots(
            pick, nav, self.cfg.kelly_scale, self.cfg.kelly_cap, self.cfg.risk_cap, self.cfg.max_lots
        )
        if lots < 1:
            self.skip_once(
                f"凯利预算 {kb.budget:.0f} / 风险上限不足一手，"
                f"区间存活={kb.p_leg:.1%} b={pick.payoff_ratio:.2f} f={kb.f:.1%}，跳过"
            )
            return
        if not self.cfg.dry_run and not cffex_session() and is_cffex_index_option(self.cfg.portfolio_name):
            self.skip_once("非交易时段，不开新仓")
            return
        self.busy = True
        try:
            buy_call = self.aggressive_price(call_long.tick, True, pick.p_call_long, call_long.vt_symbol)
            buy_put = self.aggressive_price(put_long.tick, True, pick.p_put_long, put_long.vt_symbol)
            self.send_long(call_long.vt_symbol, buy_call, lots)
            self.send_long(put_long.vt_symbol, buy_put, lots)
            if not self.cfg.dry_run:
                ok_c = self.wait_net(call_long.vt_symbol, lots, 8.0)
                ok_p = self.wait_net(put_long.vt_symbol, lots, 8.0)
                if not (ok_c and ok_p):
                    self.write("长腿未成交，撤单并回退")
                    self.cancel_symbol(call_long.vt_symbol)
                    self.cancel_symbol(put_long.vt_symbol)
                    long_c = max(int(round(self.net_pos(call_long.vt_symbol))), 0)
                    long_p = max(int(round(self.net_pos(put_long.vt_symbol))), 0)
                    if long_c:
                        self.send_sell(call_long.vt_symbol, self.aggressive_price(call_long.tick, False, buy_call, call_long.vt_symbol), long_c)
                    if long_p:
                        self.send_sell(put_long.vt_symbol, self.aggressive_price(put_long.tick, False, buy_put, put_long.vt_symbol), long_p)
                    return
            sell_call = self.aggressive_price(call.tick, False, pick.p_call, call.vt_symbol)
            sell_put = self.aggressive_price(put.tick, False, pick.p_put, put.vt_symbol)
            self.send_short(call.vt_symbol, sell_call, lots)
            self.send_short(put.vt_symbol, sell_put, lots)
            if not self.cfg.dry_run:
                ok_sc = self.wait_net(call.vt_symbol, -lots, 8.0)
                ok_sp = self.wait_net(put.vt_symbol, -lots, 8.0)
                if not (ok_sc and ok_sp):
                    self.write("短腿未成交，撤单并平掉长腿")
                    self.cancel_symbol(call.vt_symbol)
                    self.cancel_symbol(put.vt_symbol)
                    short_c = min(int(round(self.net_pos(call.vt_symbol))), 0)
                    short_p = min(int(round(self.net_pos(put.vt_symbol))), 0)
                    if short_c:
                        self.send_cover(call.vt_symbol, self.aggressive_price(call.tick, True, sell_call, call.vt_symbol), abs(short_c))
                    if short_p:
                        self.send_cover(put.vt_symbol, self.aggressive_price(put.tick, True, sell_put, put.vt_symbol), abs(short_p))
                    self.send_sell(call_long.vt_symbol, self.aggressive_price(call_long.tick, False, buy_call, call_long.vt_symbol), lots)
                    self.send_sell(put_long.vt_symbol, self.aggressive_price(put_long.tick, False, buy_put, put_long.vt_symbol), lots)
                    return
            self.book.lots = lots
            self.book.k_call = pick.k_call
            self.book.k_put = pick.k_put
            self.book.k_call_long = pick.k_call_long
            self.book.k_put_long = pick.k_put_long
            self.book.call_symbol = call.vt_symbol
            self.book.put_symbol = put.vt_symbol
            self.book.call_long_symbol = call_long.vt_symbol
            self.book.put_long_symbol = put_long.vt_symbol
            self.book.entry_call = pick.p_call
            self.book.entry_put = pick.p_put
            self.book.entry_call_long = pick.p_call_long
            self.book.entry_put_long = pick.p_put_long
            self.book.entry_credit = pick.credit
            self.write(
                f"开仓铁鹰 {lots} 手 {pick.k_put_long:.0f}/{pick.k_put:.0f}/{pick.k_call:.0f}/{pick.k_call_long:.0f} "
                f"权利金={pick.credit:.1f} 最大亏损={pick.max_loss:.0f} 存活={pick.range_prob:.1%} "
                f"b={pick.payoff_ratio:.2f} Kelly={kb.f:.1%} θ/风险={pick.efficiency:.5f}"
            )
        finally:
            self.busy = False

    def run(self) -> None:
        self.restore_state()
        self.write(
            f"启动 {self.cfg.portfolio_name} "
            f"{'模拟' if self.cfg.dry_run else '实盘'} 次月GEX铁鹰 "
            f"资金份额={self.cfg.capital_share:.0%}"
        )
        try:
            while getattr(self.engine, "strategy_active", True):
                try:
                    self.step()
                except Exception:
                    self.write("step异常\n" + traceback.format_exc())
                sleep(self.cfg.loop_interval)
        finally:
            self.write("脚本停止")
            self.publish({"active": False})

    def step(self) -> None:
        portfolio = self.portfolio()
        if not portfolio:
            self.publish({"reason": "等待期权组合"})
            return
        if not self.recovered:
            self.restore_state()
            self.recovered = True
        symbol, chain, dte = self.pick_next_month_chain(portfolio)
        if not chain:
            self.publish({"reason": "没有合适到期的期权链"})
            return
        underlying = getattr(chain, "underlying", None)
        spot = float(getattr(underlying, "mid_price", 0) or 0)
        if spot <= 0 and underlying is not None:
            spot = self.mid_price(getattr(underlying, "tick", None))
        if spot <= 0:
            self.publish({"reason": "等待标的价格"})
            return
        self.last_spot = spot
        today = datetime.now().date()
        self.update_daily_bars(spot, today)

        highs = list(self.day_highs)[-self.cfg.lsp_lookback :] or [spot]
        lows = list(self.day_lows)[-self.cfg.lsp_lookback :] or [spot]
        lsp = lsp_value(highs, lows, spot)
        hv = self.realized_hv()
        hv60 = self.realized_hv(60)
        iv = hv * 1.12
        rank = iv_rank(iv, list(self.hv_hist))
        hv_ready = len(self.day_closes) >= self.cfg.hv_lookback + 1 and len(self.hv_hist) >= 10
        iv_high = hv_ready and rank >= self.cfg.iv_rank_min
        range_ok = self.cfg.lsp_lo <= lsp <= self.cfg.lsp_hi
        expand_ok = hv <= self.cfg.hv_expand * max(hv60, 1e-6)
        dte_ok = self.cfg.min_entry_dte <= dte <= self.cfg.max_entry_dte
        session_ok = (not is_cffex_index_option(self.cfg.portfolio_name)) or cffex_session()
        expiry = today + timedelta(days=max(dte, 1))
        t = max(dte, 1) / 365.0
        step = strike_step_for(self.cfg.portfolio_name, spot, self.cfg.strike_step)
        walls = live_gex_walls(chain, spot, step)
        if walls is None:
            walls = synthetic_gex_walls(spot, iv, t, step, self.cfg.option_size)
        call_wall, put_wall = walls
        pick = pick_iron_condor(
            spot, iv, t, step, self.cfg.option_size,
            call_wall, put_wall, self.cfg.min_delta, self.cfg.max_delta,
            self.cfg.wing_steps, self.cfg.min_credit_frac, self.cfg.price_floor,
        )
        nav = self.account_nav()
        call = self.find_option(chain, self.book.k_call or (pick.k_call if pick else 0), True)
        put = self.find_option(chain, self.book.k_put or (pick.k_put if pick else 0), False)
        call_long = self.find_option(chain, self.book.k_call_long or (pick.k_call_long if pick else 0), True)
        put_long = self.find_option(chain, self.book.k_put_long or (pick.k_put_long if pick else 0), False)

        if self.book.lots > 0:
            call_px = self.mid_price(getattr(call, "tick", None), self.book.entry_call) if call else self.book.entry_call
            put_px = self.mid_price(getattr(put, "tick", None), self.book.entry_put) if put else self.book.entry_put
            call_l_px = self.mid_price(getattr(call_long, "tick", None), self.book.entry_call_long) if call_long else self.book.entry_call_long
            put_l_px = self.mid_price(getattr(put_long, "tick", None), self.book.entry_put_long) if put_long else self.book.entry_put_long
            debit = (call_px + put_px) - (call_l_px + put_l_px)
            take_profit = self.book.entry_credit > 0 and debit <= self.cfg.take_profit * self.book.entry_credit
            d_call = abs(float(getattr(call, "theo_delta", 0) or 0)) if call else 0.0
            d_put = abs(float(getattr(put, "theo_delta", 0) or 0)) if put else 0.0
            if d_call <= 0 and pick is not None:
                d_call = abs(pick.d_call)
            if d_put <= 0 and pick is not None:
                d_put = abs(pick.d_put)
            delta_hit = d_call >= self.cfg.delta_stop or d_put >= self.cfg.delta_stop
            held_dte = int(getattr(chain, "days_to_expiry", dte) or dte)
            if take_profit or delta_hit or held_dte <= self.cfg.roll_dte:
                if take_profit:
                    reason = f"止盈 收回{max(0.0, 1.0 - debit / max(self.book.entry_credit, 1e-6)):.0%}"
                elif delta_hit:
                    reason = f"短腿Δ超限 Call={d_call:.2f} Put={d_put:.2f}"
                else:
                    reason = f"移仓 DTE={held_dte}"
                self.write(reason)
                self.close_book(call, put, call_long, put_long)

        if self.book.lots <= 0 and iv_high and range_ok and expand_ok and dte_ok and session_ok and pick is not None:
            call = self.find_option(chain, pick.k_call, True)
            put = self.find_option(chain, pick.k_put, False)
            call_long = self.find_option(chain, pick.k_call_long, True)
            put_long = self.find_option(chain, pick.k_put_long, False)
            if call and put and call_long and put_long:
                self.book.expiry = expiry
                self.open_book(pick, call, put, call_long, put_long, nav)
            else:
                self.skip_once("链上找不到铁鹰四腿，跳过开仓")
        elif self.book.lots <= 0:
            reasons = []
            if not session_ok:
                reasons.append("非交易时段")
            if not hv_ready:
                reasons.append(f"HV样本不足 closes={len(self.day_closes)} hist={len(self.hv_hist)}")
            if not iv_high and hv_ready:
                reasons.append(f"IV Rank {rank:.0f}<{self.cfg.iv_rank_min:.0f}")
            if not range_ok:
                reasons.append(f"LSP {lsp:.2f} 不在[{self.cfg.lsp_lo:.2f},{self.cfg.lsp_hi:.2f}]")
            if not expand_ok:
                reasons.append("波动扩张")
            if not dte_ok:
                reasons.append(f"DTE {dte} 不在开仓窗")
            if pick is None and session_ok:
                reasons.append("选不出合格铁鹰")
            if reasons:
                self.skip_once("；".join(reasons) + "，不开新仓")

        option_delta = 0.0
        if self.book.lots > 0:
            dc = float(getattr(call, "theo_delta", 0) or 0) if call else 0.0
            dp = float(getattr(put, "theo_delta", 0) or 0) if put else 0.0
            dcl = float(getattr(call_long, "theo_delta", 0) or 0) if call_long else 0.0
            dpl = float(getattr(put_long, "theo_delta", 0) or 0) if put_long else 0.0
            if pick is not None and abs(dc) + abs(dp) <= 0:
                option_delta = self.book.lots * pick.delta * self.cfg.option_size * spot
            else:
                option_delta = self.book.lots * (-dc - dp + dcl + dpl) * self.cfg.option_size * spot
        if self.cfg.hedge and self.book.lots > 0:
            fut_target = (0.0 - option_delta) / max(spot * self.cfg.futures_size, 1.0)
            if self.cfg.hedge_symbol:
                self.send_hedge(self.cfg.hedge_symbol, fut_target, spot)
            elif underlying is not None and getattr(underlying, "exchange", None) and underlying.exchange.value != "LOCAL":
                self.send_hedge(underlying.vt_symbol, fut_target, spot)
        elif self.book.lots <= 0 and self.book.fut_lots:
            if self.cfg.hedge_symbol:
                self.send_hedge(self.cfg.hedge_symbol, 0.0, spot)
            elif underlying is not None and getattr(underlying, "exchange", None) and underlying.exchange.value != "LOCAL":
                self.send_hedge(underlying.vt_symbol, 0.0, spot)

        kelly_info = None
        if pick is not None:
            lots_est, kb = condor_lots(
                pick, nav, self.cfg.kelly_scale, self.cfg.kelly_cap, self.cfg.risk_cap, self.cfg.max_lots
            )
            kelly_info = {
                "p_call": round(kb.p_call, 4),
                "p_put": round(kb.p_put, 4),
                "p_leg": round(kb.p_leg, 4),
                "f_raw": round(kb.f_raw, 4),
                "f": round(kb.f, 4),
                "budget": round(kb.budget, 0),
                "margin": round(pick.margin, 0),
                "credit": round(pick.credit, 2),
                "max_loss": round(pick.max_loss, 0),
                "lots": lots_est,
            }
        signals = {
            "session_ok": bool(session_ok),
            "iv_high": bool(iv_high),
            "range_ok": bool(range_ok),
            "expand_ok": bool(expand_ok),
            "dte_ok": self.cfg.min_entry_dte <= dte <= self.cfg.max_entry_dte,
            "has_pick": pick is not None,
            "flat": self.book.lots <= 0,
            "reason": self.last_skip,
        }
        self.publish(
            {
                "spot": round(spot, 2),
                "lsp": round(lsp, 3),
                "iv": round(iv, 4),
                "iv_rank": round(rank, 1),
                "iv_high": iv_high,
                "range_ok": range_ok,
                "expand_ok": expand_ok,
                "dte": dte,
                "chain": symbol,
                "call_wall": call_wall,
                "put_wall": put_wall,
                "nav": round(nav, 0),
                "session_ok": session_ok,
                "reason": self.last_skip,
                "kelly": kelly_info,
                "signals": signals,
                "pick": None
                if pick is None
                else {
                    "k_put_long": pick.k_put_long,
                    "k_put": pick.k_put,
                    "k_call": pick.k_call,
                    "k_call_long": pick.k_call_long,
                    "credit": round(pick.credit, 2),
                    "range_prob": round(pick.range_prob, 4),
                    "win_prob": round(pick.win_prob, 4),
                    "efficiency": round(pick.efficiency, 6),
                },
            }
        )


class LiveBundle:
    def __init__(self, engine: ScriptEngine, configs: list[Config]) -> None:
        self.engine = engine
        self.notes: deque[str] = deque(maxlen=80)
        self.traders = [GexTvStrangle(engine, cfg, self) for cfg in configs]

    def flush(self, active: bool = True) -> None:
        payload = {
            "active": active,
            "updated": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "dry_run": all(trader.cfg.dry_run for trader in self.traders) if self.traders else True,
            "live_mode": bool(self.traders) and not any(trader.cfg.dry_run for trader in self.traders),
            "params": {
                "portfolios": [trader.cfg.portfolio_name for trader in self.traders],
                "capital_share": [trader.cfg.capital_share for trader in self.traders],
                "dry_run": [trader.cfg.dry_run for trader in self.traders],
            },
            "books": {trader.cfg.portfolio_name: trader.snapshot() for trader in self.traders},
            "decisions": list(self.notes),
        }
        first = next(iter(self.traders), None)
        if first is not None:
            payload.update(first.last_extra)
            payload["portfolio"] = first.cfg.portfolio_name
            payload["book"] = first.book_payload()
        try:
            save_json(STATUS_FILE, payload)
        except Exception as exc:
            if first is not None:
                first.write(f"状态写入失败: {exc}")

    def run(self) -> None:
        for trader in self.traders:
            trader.restore_state()
            trader.write(
                f"启动 {trader.cfg.portfolio_name} "
                f"{'模拟' if trader.cfg.dry_run else '实盘'} 次月GEX铁鹰 "
                f"资金份额={trader.cfg.capital_share:.0%}"
            )
        interval = self.traders[0].cfg.loop_interval if self.traders else 2.0
        try:
            while getattr(self.engine, "strategy_active", True):
                for trader in self.traders:
                    try:
                        trader.step()
                    except Exception:
                        trader.write("step异常\n" + traceback.format_exc())
                self.flush(True)
                sleep(interval)
        finally:
            for trader in self.traders:
                trader.write("脚本停止")
            self.flush(False)


def run(engine: ScriptEngine) -> None:
    configs = configs_from_env()
    if len(configs) > 1:
        LiveBundle(engine, configs).run()
        return
    cfg = configs[0] if configs else CFG
    GexTvStrangle(engine, cfg).run()
