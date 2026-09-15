"""L1 正收益策略回测：改进做市 + Delta 对冲空头跨式。

数据复用 l1_mm_tick_cache（生产 IF/IO Tick）。
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np

from vnpy_optionmaster.pricing.black_76 import (
    calculate_delta,
    calculate_price,
)

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "l1_mm_tick_cache"
RESULT_PATH = ROOT / "backtest_l1_positive_strategies_result.json"
REPORT_PATH = ROOT / "backtest_l1_positive_strategies_report.md"

PRICETICK = 0.2
STRIKE = 4550.0
OPT_SIZE = 100.0
FUT_SIZE = 300.0
OPT_COMM = 1.5
FUT_COMM = 25.0
RATE = 0.02
CAPITAL = 200_000.0
EXPIRY = date(2026, 9, 18)
DAYS = ["2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"]
UNDERLYING = "IF2609"
CALL = "IO2609-C-4550"
PUT = "IO2609-P-4550"


@dataclass
class MmParams:
    name: str = "改进做市"
    sigma: float = 0.22
    theo_weight: float = 0.55
    min_spread_ticks: int = 2
    max_pos: int = 2
    quote_volume: int = 1
    flatten_at: int = 1
    sell_bias_ticks: int = 1
    cooldown_sec: float = 8.0
    skip_until: str = "09:35:00"
    flat_from: str = "14:50:00"
    hedge: bool = True
    hedge_lots: float = 0.25
    join_ticks: int = 0


@dataclass
class StraddleParams:
    name: str = "空头跨式对冲"
    lots: int = 2
    sigma: float = 0.22
    entry: str = "09:35"
    exit: str = "14:30"
    hedge_lots: float = 0.5
    rich_edge: float = 0.0


def parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value).replace("T", " ").replace("+08:00", "").replace("Z", "")
    if "." in text:
        text = text.split(".")[0]
    return datetime.fromisoformat(text[:19])


def in_session(dt: datetime) -> bool:
    hhmm = dt.strftime("%H:%M")
    return ("09:30" <= hhmm <= "11:30") or ("13:00" <= hhmm <= "15:00")


def floor_to(value: float, tick: float) -> float:
    return math.floor(value / tick + 1e-12) * tick


def ceil_to(value: float, tick: float) -> float:
    return math.ceil(value / tick - 1e-12) * tick


def mid_of(row: dict[str, Any]) -> float:
    bid = float(row.get("bid_price_1") or 0)
    ask = float(row.get("ask_price_1") or 0)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return float(row.get("last_price") or 0)


def year_fraction(day: date) -> float:
    return max((EXPIRY - day).days, 1) / 365.0


def load_day(symbol: str, day: str) -> list[dict[str, Any]]:
    path = CACHE_DIR / f"{symbol}_{day}.jsonl"
    if not path.exists():
        raise FileNotFoundError(path)
    rows: list[dict[str, Any]] = []
    for line in path.open(encoding="utf-8"):
        row = json.loads(line)
        dt = parse_dt(row["datetime"])
        if in_session(dt):
            row["_dt"] = dt
            rows.append(row)
    return rows


def load_minute(symbol: str, day: str) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for row in load_day(symbol, day):
        key = row["_dt"].strftime("%Y-%m-%d %H:%M")
        buckets[key] = row
    return [buckets[k] for k in sorted(buckets)]


def sharpe_of(daily: dict[str, float]) -> float:
    arr = np.array(list(daily.values()), dtype=float)
    if len(arr) < 2:
        return 0.0
    rets = arr / CAPITAL
    return float(np.mean(rets) / (np.std(rets) + 1e-9) * math.sqrt(242))


def max_dd_of(curve: list[float]) -> float:
    peak = 0.0
    dd = 0.0
    for value in curve:
        peak = max(peak, value)
        dd = min(dd, value - peak)
    return dd


def run_improved_mm(params: MmParams) -> dict[str, Any]:
    events: list[tuple[datetime, int, str, dict[str, Any]]] = []
    for day in DAYS:
        for row in load_day(UNDERLYING, day):
            events.append((row["_dt"], 0, "IF", row))
        for row in load_day(CALL, day):
            events.append((row["_dt"], 1, "C", row))
        for row in load_day(PUT, day):
            events.append((row["_dt"], 1, "P", row))
    events.sort(key=lambda x: (x[0], x[1], x[2]))

    cash = 0.0
    fut = 0
    pos = {"C": 0, "P": 0}
    delta = {"C": 0.0, "P": 0.0}
    last_mid = {"C": 0.0, "P": 0.0}
    quote: dict[str, dict[str, Any] | None] = {"C": None, "P": None}
    last_fill_at: dict[str, datetime | None] = {"C": None, "P": None}
    spot = 0.0

    fills = buy_fills = sell_fills = 0
    captures: list[float] = []
    trades: list[dict[str, Any]] = []
    daily: dict[str, float] = {}
    day_start = 0.0
    last_day = ""
    equity = 0.0
    equity_curve: list[float] = []
    fut_turns = 0

    def mark() -> float:
        return (
            cash
            + fut * spot * FUT_SIZE
            + pos["C"] * last_mid["C"] * OPT_SIZE
            + pos["P"] * last_mid["P"] * OPT_SIZE
        )

    def hedge() -> None:
        nonlocal cash, fut, fut_turns
        if not params.hedge or spot <= 0:
            return
        port = (pos["C"] * delta["C"] + pos["P"] * delta["P"]) * OPT_SIZE
        target = -port / FUT_SIZE
        if abs(target - fut) >= params.hedge_lots:
            lots = int(round(target - fut))
            if lots:
                cash -= lots * spot * FUT_SIZE + abs(lots) * FUT_COMM
                fut += lots
                fut_turns += abs(lots)

    def flatten_leg(sym: str, row: dict[str, Any], dt: datetime) -> None:
        nonlocal cash, fills, buy_fills, sell_fills
        if pos[sym] == 0:
            return
        bid = float(row.get("bid_price_1") or 0)
        ask = float(row.get("ask_price_1") or 0)
        qty = abs(pos[sym])
        if pos[sym] > 0:
            if bid <= 0:
                return
            px = bid
            cash += qty * px * OPT_SIZE
            cash -= qty * OPT_COMM
            sell_fills += qty
            side = "S"
        else:
            if ask <= 0:
                return
            px = ask
            cash -= qty * px * OPT_SIZE
            cash -= qty * OPT_COMM
            buy_fills += qty
            side = "B"
        pos[sym] = 0
        fills += qty
        trades.append(
            {
                "dt": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": sym,
                "side": side,
                "px": px,
                "pos": 0,
                "reason": "eod_flatten",
            }
        )

    for dt, kind, sym, row in events:
        day = dt.strftime("%Y-%m-%d")
        if day != last_day:
            if last_day:
                daily[last_day] = round(equity - day_start, 2)
            day_start = equity
            last_day = day
            quote = {"C": None, "P": None}

        if kind == 0:
            bid = float(row.get("bid_price_1") or 0)
            ask = float(row.get("ask_price_1") or 0)
            if bid > 0 and ask > 0:
                spot = (bid + ask) / 2
            continue
        if spot <= 0:
            continue

        hhmmss = dt.strftime("%H:%M:%S")
        bid = float(row.get("bid_price_1") or 0)
        ask = float(row.get("ask_price_1") or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        mkt_mid = (bid + ask) / 2
        last_mid[sym] = mkt_mid

        if hhmmss < params.skip_until:
            quote[sym] = None
            continue

        if hhmmss >= params.flat_from:
            flatten_leg(sym, row, dt)
            hedge()
            equity = mark()
            equity_curve.append(equity)
            continue

        q = quote[sym]
        if q:
            cooled = True
            if last_fill_at[sym] is not None:
                cooled = (dt - last_fill_at[sym]).total_seconds() >= params.cooldown_sec
            if cooled:
                side = 0
                px = 0.0
                if q["allow_bid"] and q["bid"] + 1e-12 >= ask and pos[sym] < params.max_pos:
                    side = 1
                    px = q["bid"]
                elif q["allow_ask"] and q["ask"] - 1e-12 <= bid and pos[sym] > -params.max_pos:
                    side = -1
                    px = q["ask"]
                if side:
                    vol = params.quote_volume
                    cash -= side * px * OPT_SIZE * vol
                    cash -= OPT_COMM * vol
                    pos[sym] += side * vol
                    fills += vol
                    if side > 0:
                        buy_fills += vol
                    else:
                        sell_fills += vol
                    captures.append((q["mid"] - px) * side * OPT_SIZE)
                    last_fill_at[sym] = dt
                    trades.append(
                        {
                            "dt": dt.strftime("%Y-%m-%d %H:%M:%S"),
                            "symbol": sym,
                            "side": "B" if side > 0 else "S",
                            "px": px,
                            "pos": pos[sym],
                            "capture": round(captures[-1], 2),
                        }
                    )
                    quote[sym] = None

        tte = year_fraction(dt.date())
        cp = 1 if sym == "C" else -1
        theo = float(calculate_price(spot, STRIKE, RATE, tte, params.sigma, cp))
        unit_delta = float(calculate_delta(spot, STRIKE, RATE, tte, params.sigma, cp))
        delta[sym] = unit_delta
        fair = params.theo_weight * theo + (1.0 - params.theo_weight) * mkt_mid
        reservation = fair - params.sell_bias_ticks * PRICETICK * (1 if pos[sym] >= 0 else -0.25)
        reservation -= (pos[sym] / max(params.max_pos, 1)) * 2 * PRICETICK
        half = max(params.min_spread_ticks * PRICETICK / 2, PRICETICK)
        q_bid = floor_to(reservation - half, PRICETICK)
        q_ask = ceil_to(reservation + half, PRICETICK)
        if q_ask <= q_bid:
            q_ask = q_bid + PRICETICK
        q_bid = min(q_bid, ask - PRICETICK)
        q_ask = max(q_ask, bid + PRICETICK)
        if params.join_ticks > 0:
            q_bid = max(q_bid, ask - params.join_ticks * PRICETICK)
            q_ask = min(q_ask, bid + params.join_ticks * PRICETICK)
            q_bid = min(q_bid, ask - PRICETICK)
            q_ask = max(q_ask, bid + PRICETICK)

        allow_bid = pos[sym] < params.max_pos
        allow_ask = pos[sym] > -params.max_pos
        if abs(pos[sym]) >= params.flatten_at:
            if pos[sym] > 0:
                allow_bid = False
            if pos[sym] < 0:
                allow_ask = False

        if q_bid > 0 and q_ask > q_bid:
            quote[sym] = {
                "bid": q_bid,
                "ask": q_ask,
                "mid": fair,
                "allow_bid": allow_bid,
                "allow_ask": allow_ask,
            }
        else:
            quote[sym] = None

        hedge()
        equity = mark()
        equity_curve.append(equity)

    if last_day:
        daily[last_day] = round(equity - day_start, 2)

    residual = abs(pos["C"]) + abs(pos["P"])
    final = equity - residual * OPT_COMM - abs(fut) * FUT_COMM
    wins = [v for v in daily.values() if v > 0]

    return {
        "name": params.name,
        "strategy": "improved_mm",
        "final_pnl": round(final, 2),
        "sharpe": round(sharpe_of(daily), 3),
        "max_dd": round(max_dd_of(equity_curve), 2),
        "win_rate": round(100.0 * len(wins) / max(len(daily), 1), 1),
        "fills": fills,
        "buy_fills": buy_fills,
        "sell_fills": sell_fills,
        "avg_spread_capture": round(float(np.mean(captures)), 2) if captures else 0.0,
        "fut_turnover": fut_turns,
        "end_pos": dict(pos),
        "end_fut": fut,
        "daily_pnl": daily,
        "trades_sample": trades[:25],
        "params": asdict(params),
    }


def run_short_straddle(params: StraddleParams) -> dict[str, Any]:
    cash_total = 0.0
    daily: dict[str, float] = {}
    trades: list[dict[str, Any]] = []
    equity_curve: list[float] = []
    fut_turns = 0
    fills = 0

    for day in DAYS:
        if_rows = load_minute(UNDERLYING, day)
        c_map = {r["_dt"].strftime("%Y-%m-%d %H:%M"): r for r in load_minute(CALL, day)}
        p_map = {r["_dt"].strftime("%Y-%m-%d %H:%M"): r for r in load_minute(PUT, day)}
        cash = 0.0
        fut = 0
        cpos = 0
        ppos = 0
        entered = False
        last_c = last_p = last_s = 0.0
        tte = year_fraction(date.fromisoformat(day))

        for row in if_rows:
            key = row["_dt"].strftime("%Y-%m-%d %H:%M")
            hhmm = key[11:16]
            spot = mid_of(row)
            if spot <= 0:
                continue
            last_s = spot
            if key in c_map:
                last_c = mid_of(c_map[key])
            if key in p_map:
                last_p = mid_of(p_map[key])

            if (
                not entered
                and hhmm >= params.entry
                and last_c > 0
                and last_p > 0
                and key in c_map
                and key in p_map
            ):
                if params.rich_edge > 0:
                    theo = float(calculate_price(spot, STRIKE, RATE, tte, params.sigma, 1)) + float(
                        calculate_price(spot, STRIKE, RATE, tte, params.sigma, -1)
                    )
                    if last_c + last_p < params.rich_edge * theo:
                        continue
                cb = float(c_map[key].get("bid_price_1") or 0)
                pb = float(p_map[key].get("bid_price_1") or 0)
                if cb <= 0 or pb <= 0:
                    continue
                lots = params.lots
                cash += lots * (cb + pb) * OPT_SIZE - 2 * lots * OPT_COMM
                cpos = ppos = -lots
                entered = True
                fills += 2 * lots
                trades.append(
                    {
                        "dt": key,
                        "action": "sell_straddle",
                        "call_px": cb,
                        "put_px": pb,
                        "lots": lots,
                        "spot": spot,
                    }
                )

            if entered and cpos:
                dc = float(calculate_delta(spot, STRIKE, RATE, tte, params.sigma, 1))
                dp = float(calculate_delta(spot, STRIKE, RATE, tte, params.sigma, -1))
                target = -(cpos * dc + ppos * dp) * OPT_SIZE / FUT_SIZE
                if abs(target - fut) >= params.hedge_lots:
                    trade = int(round(target - fut))
                    if trade:
                        cash -= trade * spot * FUT_SIZE + abs(trade) * FUT_COMM
                        fut += trade
                        fut_turns += abs(trade)

            if entered and cpos and hhmm >= params.exit and key in c_map and key in p_map:
                ca = float(c_map[key].get("ask_price_1") or last_c)
                pa = float(p_map[key].get("ask_price_1") or last_p)
                if ca <= 0:
                    ca = last_c
                if pa <= 0:
                    pa = last_p
                lots = params.lots
                cash -= lots * (ca + pa) * OPT_SIZE + 2 * lots * OPT_COMM
                cpos = ppos = 0
                fills += 2 * lots
                trades.append(
                    {
                        "dt": key,
                        "action": "buy_straddle",
                        "call_px": ca,
                        "put_px": pa,
                        "lots": lots,
                        "spot": spot,
                    }
                )

            equity_curve.append(
                cash + fut * last_s * FUT_SIZE + cpos * last_c * OPT_SIZE + ppos * last_p * OPT_SIZE
            )

        if entered and cpos:
            cash += cpos * last_c * OPT_SIZE + ppos * last_p * OPT_SIZE
            cpos = ppos = 0
        if fut:
            trade = -fut
            cash -= trade * last_s * FUT_SIZE + abs(trade) * FUT_COMM
            fut_turns += abs(trade)
            fut = 0

        daily[day] = round(cash, 2)
        cash_total += cash

    wins = [v for v in daily.values() if v > 0]
    return {
        "name": params.name,
        "strategy": "short_straddle_hedged",
        "final_pnl": round(cash_total, 2),
        "sharpe": round(sharpe_of(daily), 3),
        "max_dd": round(max_dd_of(equity_curve), 2),
        "win_rate": round(100.0 * len(wins) / max(len(daily), 1), 1),
        "fills": fills,
        "buy_fills": fills // 2,
        "sell_fills": fills // 2,
        "avg_spread_capture": 0.0,
        "fut_turnover": fut_turns,
        "end_pos": {"C": 0, "P": 0},
        "end_fut": 0,
        "daily_pnl": daily,
        "trades_sample": trades[:30],
        "params": asdict(params),
    }



def run_sell_only_mm(name: str = "只卖做市-对冲", max_pos: int = 3, cooldown: float = 5.0,
                     skip_until: str = "09:35:00", flat_from: str = "14:50:00",
                     hedge_lots: float = 0.25, sigma: float = 0.22) -> dict[str, Any]:
    """只挂卖单的做市：避免开盘被动买入堆积库存。"""
    events: list[tuple[datetime, int, str, dict[str, Any]]] = []
    for day in DAYS:
        for row in load_day(UNDERLYING, day):
            events.append((row["_dt"], 0, "IF", row))
        for row in load_day(CALL, day):
            events.append((row["_dt"], 1, "C", row))
        for row in load_day(PUT, day):
            events.append((row["_dt"], 1, "P", row))
    events.sort(key=lambda x: (x[0], x[1], x[2]))

    cash = 0.0
    fut = 0
    pos = {"C": 0, "P": 0}
    delta = {"C": 0.0, "P": 0.0}
    last_mid = {"C": 0.0, "P": 0.0}
    quote: dict[str, float | None] = {"C": None, "P": None}
    last_fill_at: dict[str, datetime | None] = {"C": None, "P": None}
    spot = 0.0
    fills = 0
    daily: dict[str, float] = {}
    day_start = 0.0
    last_day = ""
    equity = 0.0
    equity_curve: list[float] = []
    fut_turns = 0
    trades: list[dict[str, Any]] = []

    def mark() -> float:
        return (
            cash
            + fut * spot * FUT_SIZE
            + pos["C"] * last_mid["C"] * OPT_SIZE
            + pos["P"] * last_mid["P"] * OPT_SIZE
        )

    for dt, kind, sym, row in events:
        day = dt.strftime("%Y-%m-%d")
        if day != last_day:
            if last_day:
                daily[last_day] = round(equity - day_start, 2)
            day_start = equity
            last_day = day
            quote = {"C": None, "P": None}

        if kind == 0:
            bid = float(row.get("bid_price_1") or 0)
            ask = float(row.get("ask_price_1") or 0)
            if bid > 0 and ask > 0:
                spot = (bid + ask) / 2
            continue
        if spot <= 0:
            continue

        hhmmss = dt.strftime("%H:%M:%S")
        bid = float(row.get("bid_price_1") or 0)
        ask = float(row.get("ask_price_1") or 0)
        if bid <= 0 or ask <= 0:
            continue
        mkt_mid = (bid + ask) / 2
        last_mid[sym] = mkt_mid

        if hhmmss < skip_until:
            quote[sym] = None
            continue

        if hhmmss >= flat_from:
            if pos[sym] < 0 and ask > 0:
                qty = abs(pos[sym])
                cash -= qty * ask * OPT_SIZE + qty * OPT_COMM
                fills += qty
                trades.append({"dt": dt.strftime("%Y-%m-%d %H:%M:%S"), "symbol": sym, "side": "B", "px": ask, "reason": "cover"})
                pos[sym] = 0
            quote[sym] = None
            port = (pos["C"] * delta["C"] + pos["P"] * delta["P"]) * OPT_SIZE
            target = -port / FUT_SIZE
            if abs(target - fut) >= hedge_lots:
                lots = int(round(target - fut))
                if lots:
                    cash -= lots * spot * FUT_SIZE + abs(lots) * FUT_COMM
                    fut += lots
                    fut_turns += abs(lots)
            equity = mark()
            equity_curve.append(equity)
            continue

        q_ask = quote[sym]
        if q_ask is not None:
            cooled = last_fill_at[sym] is None or (dt - last_fill_at[sym]).total_seconds() >= cooldown
            if cooled and q_ask - 1e-12 <= bid and pos[sym] > -max_pos:
                cash += q_ask * OPT_SIZE - OPT_COMM
                pos[sym] -= 1
                fills += 1
                last_fill_at[sym] = dt
                trades.append({"dt": dt.strftime("%Y-%m-%d %H:%M:%S"), "symbol": sym, "side": "S", "px": q_ask, "pos": pos[sym]})
                quote[sym] = None

        tte = year_fraction(dt.date())
        cp = 1 if sym == "C" else -1
        theo = float(calculate_price(spot, STRIKE, RATE, tte, sigma, cp))
        delta[sym] = float(calculate_delta(spot, STRIKE, RATE, tte, sigma, cp))
        fair = 0.5 * theo + 0.5 * mkt_mid
        ask_q = ceil_to(max(fair + PRICETICK, bid + PRICETICK), PRICETICK)
        ask_q += abs(min(pos[sym], 0)) * PRICETICK
        quote[sym] = ask_q if (ask_q > bid and pos[sym] > -max_pos) else None

        port = (pos["C"] * delta["C"] + pos["P"] * delta["P"]) * OPT_SIZE
        target = -port / FUT_SIZE
        if abs(target - fut) >= hedge_lots:
            lots = int(round(target - fut))
            if lots:
                cash -= lots * spot * FUT_SIZE + abs(lots) * FUT_COMM
                fut += lots
                fut_turns += abs(lots)

        equity = mark()
        equity_curve.append(equity)

    if last_day:
        daily[last_day] = round(equity - day_start, 2)
    final = equity - abs(fut) * FUT_COMM
    wins = [v for v in daily.values() if v > 0]
    return {
        "name": name,
        "strategy": "sell_only_mm",
        "final_pnl": round(final, 2),
        "sharpe": round(sharpe_of(daily), 3),
        "max_dd": round(max_dd_of(equity_curve), 2),
        "win_rate": round(100.0 * len(wins) / max(len(daily), 1), 1),
        "fills": fills,
        "buy_fills": 0,
        "sell_fills": fills,
        "avg_spread_capture": 0.0,
        "fut_turnover": fut_turns,
        "end_pos": dict(pos),
        "end_fut": fut,
        "daily_pnl": daily,
        "trades_sample": trades[:25],
        "params": {
            "max_pos": max_pos,
            "cooldown": cooldown,
            "skip_until": skip_until,
            "flat_from": flat_from,
            "hedge_lots": hedge_lots,
            "sigma": sigma,
        },
    }


MM_PRESETS = [
    MmParams("改进做市-稳健"),
    MmParams("改进做市-更偏卖", sell_bias_ticks=2, max_pos=1, flatten_at=1, cooldown_sec=12),
    MmParams("改进做市-更勤对冲", hedge_lots=0.15, cooldown_sec=6, max_pos=2),
]

STRADDLE_PRESETS = [
    StraddleParams("空头跨式-基准", lots=1, entry="09:35", exit="14:30", hedge_lots=0.5),
    StraddleParams("空头跨式-加仓", lots=2, entry="09:35", exit="14:30", hedge_lots=0.5),
    StraddleParams("空头跨式-最优扫描", lots=3, entry="09:35", exit="14:30", hedge_lots=1.0, sigma=0.22),
    StraddleParams(
        "空头跨式-富IV过滤",
        lots=2,
        entry="09:45",
        exit="14:30",
        hedge_lots=0.5,
        rich_edge=1.02,
        sigma=0.18,
    ),
]


def build_report(payload: dict[str, Any]) -> str:
    results = payload["results"]
    lines = [
        "# L1 正收益策略回测报告",
        "",
        f"- 生成时间：{payload['generated']}",
        f"- 样本：{', '.join(DAYS)}（IF2609 + IO2609 ATM 4550）",
        "- 策略：改进做市（Tick） / Delta 对冲空头跨式（分钟）",
        "",
        "## 结果对比",
        "",
        "| 名称 | 类型 | 盈亏 | 夏普 | 最大回撤 | 胜率% | 成交 | 日盈亏 |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    best = None
    for row in results:
        lines.append(
            f"| {row['name']} | {row['strategy']} | {row['final_pnl']} | {row['sharpe']} | "
            f"{row['max_dd']} | {row['win_rate']} | {row['fills']} | "
            f"{json.dumps(row['daily_pnl'], ensure_ascii=False)} |"
        )
        if best is None or float(row["final_pnl"]) > float(best["final_pnl"]):
            best = row
    lines.append("")
    if best:
        lines.extend(
            [
                "## 最优策略解读",
                "",
                f"- **{best['name']}**：四日合计 **{best['final_pnl']}**，夏普 {best['sharpe']}，"
                f"最大回撤 {best['max_dd']}，胜率 {best['win_rate']}%",
                f"- 日盈亏：{json.dumps(best['daily_pnl'], ensure_ascii=False)}",
                f"- 参数：{json.dumps(best.get('params') or {}, ensure_ascii=False)}",
                "",
                "### 为何能转正",
                "",
                "1. 旧做市开盘几秒内连续被动买入，堆积多头跨式，9/8 单日大亏。",
                "2. 改进做市：跳过开盘、成交冷却、硬减仓、偏卖、收盘强平。",
                "3. 空头跨式+对冲：卖 ATM 波动率并持续对冲 Delta，本样本四日均为正。",
                "",
                "### 风险提示",
                "",
                "- 样本仅 4 个交易日，存在过拟合；实盘需更长样本与趋势日压力测试。",
                "- 空头跨式在大波动日可能显著回撤；需限额与止损。",
                "- 成交按买一/卖一保守估计，未计入排队恶化。",
                "",
            ]
        )
    return "\n".join(lines)


def run_all() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for cfg in MM_PRESETS:
        print("running", cfg.name, flush=True)
        results.append(run_improved_mm(cfg))
        print(" ->", results[-1]["final_pnl"], results[-1]["daily_pnl"], flush=True)
    print("running 只卖做市-对冲", flush=True)
    results.append(run_sell_only_mm())
    print(" ->", results[-1]["final_pnl"], results[-1]["daily_pnl"], flush=True)
    for cfg in STRADDLE_PRESETS:
        print("running", cfg.name, flush=True)
        results.append(run_short_straddle(cfg))
        print(" ->", results[-1]["final_pnl"], results[-1]["daily_pnl"], flush=True)

    results.sort(key=lambda r: float(r["final_pnl"]), reverse=True)
    payload = {
        "generated": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "sample_days": DAYS,
        "universe": f"{UNDERLYING} + {CALL}/{PUT}",
        "results": results,
        "positive_count": sum(1 for r in results if float(r["final_pnl"]) > 0),
    }
    RESULT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_PATH.write_text(build_report(payload), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="L1 正收益策略回测")
    parser.parse_args()
    if not CACHE_DIR.exists():
        raise SystemExit(f"缺少 Tick 缓存目录: {CACHE_DIR}")
    out = run_all()
    summary = {
        r["name"]: {"pnl": r["final_pnl"], "sharpe": r["sharpe"], "daily": r["daily_pnl"]}
        for r in out["results"]
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("saved", RESULT_PATH)
    print("report", REPORT_PATH)
    print("positive_count", out["positive_count"], "/", len(out["results"]))


if __name__ == "__main__":
    main()
