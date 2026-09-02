"""Backtest IO covered-call mispricing harvester on IF bars.

Long IF + short synthetic IO call when bid is rich vs Black-76 theo.
Cover ratio 3 IO : 1 IF. No historical option L2 — book is synthetic from
theo ± spread with injected rich-bid events (tick-like substeps inside each bar).
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from vnpy_optionmaster.pricing.black_76 import calculate_delta, calculate_price

ROOT = Path(__file__).resolve().parent
CACHE_5M = ROOT.joinpath("if_5min_cache.json")
CACHE_1D = ROOT.joinpath("if_daily_cache.json")
RESULT_PATH = ROOT.joinpath("io_covered_call_if_report.json")

OPT_SIZE = 100.0
FUT_SIZE = 300.0
OPT_COMM = 1.5
FUT_COMM = 25.0
PRICETICK = 0.2
RATE = 0.02
CAPITAL = 1_000_000.0


@dataclass
class Params:
    name: str
    min_delta: float = 0.18
    max_delta: float = 0.32
    target_dte: int = 28
    edge_ticks: float = 2.0
    min_credit: float = 0.8
    call_lots: int = 3
    cover_ratio: int = 3
    take_profit: float = 0.45
    delta_stop: float = 0.55
    roll_dte: int = 7
    stop_if_pct: float = 0.025
    risk_cap: float = 0.08
    max_books: int = 1
    # synthetic microstructure
    half_spread_ticks: float = 1.0
    rich_prob: float = 0.08  # chance a bar's call bid is rich
    rich_extra_ticks: float = 3.0
    ticks_per_bar: int = 3  # synthetic tick steps inside bar
    flat_eod: bool = False
    iv_premium: float = 0.08
    hv_lookback: int = 20


PRESETS: list[Params] = [
    Params(
        "推荐-日内错价备兑",
        flat_eod=True,
        min_delta=0.12,
        max_delta=0.40,
        edge_ticks=2.5,
        rich_prob=0.10,
        rich_extra_ticks=3.5,
        stop_if_pct=0.015,
        take_profit=0.50,
        call_lots=3,
    ),
    Params("积极6手-日内", flat_eod=True, call_lots=6, min_delta=0.12, max_delta=0.40, edge_ticks=2.5, rich_prob=0.10, stop_if_pct=0.015),
    Params("更严错价-日内", flat_eod=True, edge_ticks=3.5, rich_prob=0.07, rich_extra_ticks=4.5, stop_if_pct=0.015, call_lots=3),
    Params("隔夜备兑", flat_eod=False, edge_ticks=2.5, rich_prob=0.08, stop_if_pct=0.02, call_lots=3),
    Params("窄Δ-日内", flat_eod=True, min_delta=0.18, max_delta=0.32, edge_ticks=2.5, rich_prob=0.10, stop_if_pct=0.015, call_lots=3),
]


def preset_payloads() -> list[dict[str, Any]]:
    return [asdict(item) for item in PRESETS]


def round_to(value: float, tick: float) -> float:
    return round(value / tick) * tick


def io_strike_step(spot: float) -> float:
    if spot < 2500:
        return 25.0
    if spot < 5000:
        return 50.0
    return 100.0


def load_bars(interval: str = "5m") -> list[dict[str, Any]]:
    path = CACHE_5M if interval == "5m" else CACHE_1D
    if not path.exists() or path.stat().st_size < 100:
        raise RuntimeError(f"缺少行情缓存: {path.name}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: list[dict[str, Any]] = []
    for row in raw:
        close = float(row[4])
        if close <= 0:
            continue
        stamp = str(row[0])
        if interval == "5m" and stamp[:10] < "2019-12-01":
            continue
        if interval == "1d" and stamp[:10] < "2019-12-01":
            continue
        item = {
            "datetime": stamp if " " in stamp else stamp + " 15:00:00",
            "date": stamp[:10],
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": close,
            "volume": float(row[5] or 0) if len(row) > 5 else 0.0,
        }
        hhmm = item["datetime"][11:16]
        if interval == "5m" and not (("09:30" <= hhmm <= "11:30") or ("13:00" <= hhmm <= "15:00")):
            continue
        out.append(item)
    if len(out) < 200:
        raise RuntimeError(f"样本不足: {len(out)}")
    return out


def hv_from_closes(closes: list[float], lookback: int) -> float:
    if len(closes) <= lookback:
        return 0.20
    arr = np.array(closes[-(lookback + 1) :], dtype=float)
    rets = np.diff(np.log(arr))
    if len(rets) < 5:
        return 0.20
    return float(max(0.10, min(0.80, np.std(rets, ddof=1) * math.sqrt(242))))


def next_month_expiry(day: str) -> datetime:
    """Approximate CFFEX third Friday of next month."""
    dt = datetime.strptime(day[:10], "%Y-%m-%d")
    year, month = dt.year, dt.month + 1
    if month > 12:
        year, month = year + 1, 1
    # find first day, then third Friday
    d0 = datetime(year, month, 1)
    # weekday Mon=0 ... Fri=4
    first_friday = 1 + (4 - d0.weekday()) % 7
    third = first_friday + 14
    return datetime(year, month, third)


def pick_call_strike(spot: float, iv: float, t: float, min_delta: float, max_delta: float) -> tuple[float, float, float]:
    step = io_strike_step(spot)
    atm = max(step, round(spot / step) * step)
    best = None
    k = atm
    for _ in range(24):
        k += step
        px = max(calculate_price(spot, k, RATE, max(t, 1 / 365), max(iv, 0.05), 1), PRICETICK)
        delta = abs(calculate_delta(spot, k, RATE, max(t, 1 / 365), max(iv, 0.05), 1))
        if min_delta <= delta <= max_delta:
            score = abs(delta - 0.5 * (min_delta + max_delta))
            cand = (k, float(px), float(delta), score)
            if best is None or cand[3] < best[3]:
                best = cand
    if best is None:
        k = atm + 2 * step
        px = max(calculate_price(spot, k, RATE, max(t, 1 / 365), max(iv, 0.05), 1), PRICETICK)
        delta = abs(calculate_delta(spot, k, RATE, max(t, 1 / 365), max(iv, 0.05), 1))
        return float(k), float(px), float(delta)
    return best[0], best[1], best[2]


def calendar_years(start: str, end: str) -> float:
    a = datetime.strptime(start[:10], "%Y-%m-%d")
    b = datetime.strptime(end[:10], "%Y-%m-%d")
    return max((b - a).days / 365.25, 1e-9)


def run_one(bars: list[dict[str, Any]], params: Params, seed: int = 42) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    cash = 0.0
    fut = 0
    call = 0
    k_call = 0.0
    entry_credit = 0.0
    entry_fut = 0.0
    expiry = ""
    last = float(bars[0]["close"])
    day_nav: dict[str, float] = {}
    day_closes: list[float] = []
    prev_day = ""
    opens = closes_tp = closes_delta = closes_roll = closes_stop = closes_eod = 0
    trades: list[dict[str, Any]] = []
    peak = CAPITAL
    max_dd_peak_pct = 0.0
    rich_hits = 0

    def mark(px: float) -> None:
        nonlocal cash, last
        if fut:
            cash += fut * (px - last) * FUT_SIZE
        last = px

    def call_mark(spot: float, iv: float, day: str) -> tuple[float, float]:
        if call <= 0 or k_call <= 0 or not expiry:
            return 0.0, 0.0
        dte = max((datetime.strptime(expiry, "%Y-%m-%d") - datetime.strptime(day, "%Y-%m-%d")).days, 1)
        t = dte / 365.0
        px = max(calculate_price(spot, k_call, RATE, t, max(iv, 0.05), 1), PRICETICK)
        delta = abs(calculate_delta(spot, k_call, RATE, t, max(iv, 0.05), 1))
        return float(px), float(delta)

    def flatten_all(spot: float, iv: float, day: str, stamp: str, reason: str, keep_fut: bool = False) -> None:
        nonlocal cash, fut, call, entry_credit, entry_fut, k_call, expiry
        nonlocal closes_tp, closes_delta, closes_roll, closes_stop, closes_eod
        if call > 0:
            px, _ = call_mark(spot, iv, day)
            debit = px + params.half_spread_ticks * PRICETICK
            cash -= call * debit * OPT_SIZE
            cash -= call * OPT_COMM
            trades.append({"date": stamp, "action": reason, "call": call, "debit": round(debit, 2)})
            if "止盈" in reason:
                closes_tp += 1
            elif "Delta" in reason:
                closes_delta += 1
            elif "移仓" in reason:
                closes_roll += 1
            elif "IF" in reason:
                closes_stop += 1
            else:
                closes_eod += 1
            call = 0
            entry_credit = 0.0
            k_call = 0.0
            expiry = ""
        drop_fut = (not keep_fut) or ("IF" in reason) or ("尾盘" in reason) or (reason == "结束")
        if fut > 0 and drop_fut:
            mark(spot)
            cash -= fut * FUT_COMM
            trades.append({"date": stamp, "action": f"{reason}-平IF", "fut": fut, "price": round(spot, 1)})
            fut = 0
            entry_fut = 0.0

    for i, row in enumerate(bars):
        day = row["date"]
        stamp = row["datetime"]
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        if day != prev_day:
            if prev_day and day_closes:
                pass
            if prev_day:
                day_closes.append(last)
            prev_day = day
        mark(o)

        # synthetic ticks across OHLC path
        path = np.linspace(o, c, max(params.ticks_per_bar, 2))
        # inject high/low extremes
        if len(path) >= 4:
            path[1] = h
            path[2] = l
        hv = hv_from_closes(day_closes + [c], params.hv_lookback)
        iv = hv * (1.0 + params.iv_premium)

        for px in path:
            mark(float(px))
            theo_px = 0.0
            delta = 0.0
            if call > 0:
                theo_px, delta = call_mark(float(px), iv, day)
                mid = theo_px
                ask = mid + params.half_spread_ticks * PRICETICK
                dte = max((datetime.strptime(expiry, "%Y-%m-%d") - datetime.strptime(day, "%Y-%m-%d")).days, 0)
                take_profit = entry_credit > 0 and ask <= params.take_profit * entry_credit
                delta_hit = delta >= params.delta_stop
                roll = dte <= params.roll_dte
                fut_stop = entry_fut > 0 and float(px) <= entry_fut * (1.0 - params.stop_if_pct)
                eod = params.flat_eod and stamp[11:16] >= "14:50"
                if take_profit or delta_hit or roll or fut_stop or eod:
                    reason = (
                        "止盈"
                        if take_profit
                        else (
                            "Delta止损"
                            if delta_hit
                            else ("移仓" if roll else ("IF止损" if fut_stop else "尾盘平仓"))
                        )
                    )
                    # 日内模式整组离场；隔夜模式止盈可留 IF
                    keep = (not params.flat_eod) and reason == "止盈"
                    flatten_all(float(px), iv, day, stamp, reason, keep_fut=keep)
                    break

            if call > 0:
                continue
            # entry attempt on this tick
            exp = next_month_expiry(day)
            dte = max((exp - datetime.strptime(day, "%Y-%m-%d")).days, 1)
            if dte < 10 or dte > 50:
                continue
            t = dte / 365.0
            strike, theo, delta = pick_call_strike(float(px), iv, t, params.min_delta, params.max_delta)
            half = params.half_spread_ticks * PRICETICK
            mid = theo
            bid = max(PRICETICK, mid - half)
            if rng.random() < params.rich_prob / max(params.ticks_per_bar, 1):
                bid = theo + params.edge_ticks * PRICETICK + params.rich_extra_ticks * PRICETICK
                bid = round_to(bid, PRICETICK)
                rich_hits += 1
            edge = bid - theo
            if edge < params.edge_ticks * PRICETICK or bid < params.min_credit:
                continue
            if not (params.min_delta <= delta <= params.max_delta):
                continue
            call_lots = int(params.call_lots)
            need_fut = max(1, int(math.ceil(call_lots / max(params.cover_ratio, 1))))
            mark(float(px))
            if fut < need_fut:
                buy = need_fut - fut
                cash -= buy * FUT_COMM
                fut += buy
                if entry_fut <= 0:
                    entry_fut = float(px)
            elif fut > need_fut:
                # shrink leftover cover from previous cycle
                sell = fut - need_fut
                cash -= sell * FUT_COMM
                fut -= sell
            cash += call_lots * bid * OPT_SIZE
            cash -= call_lots * OPT_COMM
            call = call_lots
            k_call = strike
            entry_credit = bid
            expiry = exp.strftime("%Y-%m-%d")
            opens += 1
            trades.append(
                {
                    "date": stamp,
                    "action": "开备兑",
                    "fut": fut,
                    "call": call_lots,
                    "strike": strike,
                    "credit": round(bid, 2),
                    "theo": round(theo, 2),
                    "edge": round(edge, 2),
                    "delta": round(delta, 3),
                    "dte": dte,
                }
            )
            break

        # end-of-bar MTM for short call liability
        mtm_call = 0.0
        if call > 0:
            px_c, _ = call_mark(c, iv, day)
            mtm_call = -call * px_c * OPT_SIZE
        mark(c)
        nav = CAPITAL + cash + mtm_call
        day_nav[day] = nav
        peak = max(peak, nav)
        if peak > 0:
            max_dd_peak_pct = min(max_dd_peak_pct, (nav - peak) / peak)

    if call or fut:
        flatten_all(float(bars[-1]["close"]), 0.2, bars[-1]["date"], bars[-1]["datetime"], "结束")
        day_nav[bars[-1]["date"]] = CAPITAL + cash

    keys = sorted(day_nav)
    eq = np.array([day_nav[k] for k in keys], dtype=float)
    pnl = np.r_[eq[0] - CAPITAL, np.diff(eq)]
    rets = pnl / np.maximum(np.r_[CAPITAL, eq[:-1]], 1.0)
    sharpe = float(np.mean(rets) / (np.std(rets) + 1e-9) * math.sqrt(242)) if len(rets) > 2 else 0.0
    years = calendar_years(keys[0], keys[-1])
    final_nav = float(eq[-1])
    cagr = (max(final_nav, 1e-9) / CAPITAL) ** (1.0 / years) - 1.0
    calmar = (cagr * 100) / abs(max_dd_peak_pct * 100) if abs(max_dd_peak_pct) > 1e-9 else 0.0
    yearly: dict[str, float] = {}
    for day, value in zip(keys, pnl):
        yearly[day[:4]] = round(yearly.get(day[:4], 0.0) + float(value), 2)
    pos_years = sum(1 for v in yearly.values() if v > 0)
    step = max(1, len(keys) // 48)
    return {
        "name": params.name,
        "start": bars[0]["datetime"],
        "end": bars[-1]["datetime"],
        "bars": len(bars),
        "days": len(keys),
        "years": round(years, 3),
        "cagr": round(cagr * 100, 2),
        "final_pnl": round(final_nav - CAPITAL, 2),
        "final_nav": round(final_nav, 2),
        "sharpe": round(sharpe, 3),
        "calmar": round(calmar, 3),
        "max_dd_peak_pct": round(100.0 * max_dd_peak_pct, 2),
        "opens": opens,
        "closes_tp": closes_tp,
        "closes_delta": closes_delta,
        "closes_roll": closes_roll,
        "closes_stop": closes_stop,
        "closes_eod": closes_eod,
        "rich_hits": rich_hits,
        "pos_year_pct": round(100.0 * pos_years / max(len(yearly), 1), 1),
        "yearly": yearly,
        "equity_x": [keys[i][2:7] for i in range(0, len(keys), step)],
        "equity_y": [round(float(eq[i]) - CAPITAL, 1) for i in range(0, len(keys), step)],
        "trades": trades[-40:],
        **asdict(params),
    }


def run_backtest(interval: str = "5m", compare: bool = True) -> dict[str, Any]:
    bars = load_bars(interval)
    variants = list(PRESETS) if compare else PRESETS[:1]
    results = [run_one(bars, item, seed=42 + i) for i, item in enumerate(variants)]
    lead = results[0]
    # walk forward on lead params
    is_bars = [b for b in bars if b["date"] <= "2023-12-31"]
    oos_bars = [b for b in bars if b["date"] >= "2024-01-01"]
    out = {
        "generated": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "engine": "io_covered_call",
        "kind": "IF",
        "universe": f"中金所IF {interval} → IO 备兑看涨错价收割",
        "interval": interval,
        "capital": CAPITAL,
        "assumptions": {
            "structure": "Long IF + Short IO Call（备兑）",
            "cover_ratio": f"{lead['cover_ratio']} IO : 1 IF",
            "entry": f"合成盘口 bid-theo ≥ {lead['edge_ticks']}tick，Δ∈[{lead['min_delta']},{lead['max_delta']}]",
            "exit": f"权利金收回至{100*(1-lead['take_profit']):.0f}% / Δ≥{lead['delta_stop']} / DTE≤{lead['roll_dte']} / IF回撤{100*lead['stop_if_pct']:.1f}%",
            "opt_commission": OPT_COMM,
            "fut_commission": FUT_COMM,
            "pricetick": PRICETICK,
            "note": "无历史期权L2；用 Black-76 合成盘口并注入错价事件，验证备兑收割逻辑与风控",
            "source": "if_5min_cache.json" if interval == "5m" else "if_daily_cache.json",
        },
        "sample": {
            "start": bars[0]["datetime"],
            "end": bars[-1]["datetime"],
            "bars": len(bars),
            "days": len({b['date'] for b in bars}),
        },
        "walk_forward": {
            "in_sample": {
                k: run_one(is_bars, PRESETS[0], seed=7)[k]
                for k in ("cagr", "sharpe", "max_dd_peak_pct", "opens", "final_pnl")
            },
            "out_of_sample": {
                k: run_one(oos_bars, PRESETS[0], seed=7)[k]
                for k in ("cagr", "sharpe", "max_dd_peak_pct", "opens", "final_pnl")
            },
        },
        "recommend": results[0]["name"],
        "results": results,
    }
    RESULT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> None:
    interval = "5m"
    for arg in sys.argv[1:]:
        if arg in ("1d", "d", "daily"):
            interval = "1d"
        elif arg in ("5m", "5"):
            interval = "5m"
    out = run_backtest(interval=interval, compare=True)
    print(f"{out['sample']['start']} → {out['sample']['end']}  bars={out['sample']['bars']}")
    print("walk_forward", out["walk_forward"])
    for row in out["results"]:
        print(
            f"{row['name']}: CAGR={row['cagr']:.2f}% Sharpe={row['sharpe']:.2f} "
            f"DD={row['max_dd_peak_pct']:.2f}% 开仓={row['opens']} "
            f"止盈={row['closes_tp']} Δ止损={row['closes_delta']} 移仓={row['closes_roll']} "
            f"IF止损={row['closes_stop']}"
        )
    print(f"结果写入 {RESULT_PATH}")


if __name__ == "__main__":
    main()
