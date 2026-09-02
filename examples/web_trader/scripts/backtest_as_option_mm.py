"""Replay AS option MM on CSI 300 30-minute bars with a synthetic ATM chain.

Uses the same reservation/spread formulas as as_option_mm.py.
Underlying path: AkShare 000300 30-minute OHLC (cached locally).
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np
from vnpy_optionmaster.pricing.black_76 import (
    calculate_delta,
    calculate_gamma,
    calculate_price,
    calculate_vega,
)


PRICETICK = 0.2
STRIKE_STEP = 50.0
OPT_SIZE = 100.0
FUT_SIZE = 300.0
OPT_COMM = 1.5
FUT_COMM = 25.0
DAILY_STEPS_REF = 24  # previous synthetic-day model, used only to scale fill odds
SEED = 42
FILL_P0 = 0.20
MAX_COMBOS = 120
CHART_TOP = 5
MINUTE_CACHE = Path(__file__).with_name("csi300_30min_cache.json")
RESULT_PATH = Path(__file__).with_name("backtest_as_option_mm_result.json")
CHART_BINS = 420


def as_quotes(
    mid: float, inventory: float, gamma: float, kappa: float, sigma: float, tau_days: float, spread_mult: float
) -> tuple[float, float]:
    gamma = max(gamma, 1e-6)
    kappa = max(kappa, 1e-6)
    sigma = max(sigma, 1e-4)
    tau = max(tau_days, 1 / 365) / 365.0
    q = max(-1.0, min(1.0, inventory))
    reservation = mid - q * gamma * (sigma ** 2) * tau * mid
    half = 0.5 * gamma * (sigma ** 2) * tau * mid + (1.0 / gamma) * math.log(1.0 + gamma / kappa) * mid * spread_mult
    return reservation, max(half, 0.0)


def round_to(value: float, tick: float) -> float:
    return round(value / tick) * tick


def floor_to(value: float, tick: float) -> float:
    return math.floor(value / tick + 1e-12) * tick


def ceil_to(value: float, tick: float) -> float:
    return math.ceil(value / tick - 1e-12) * tick


def greeks(s: float, k: float, t: float, v: float, cp: int) -> tuple[float, float, float, float]:
    t = max(t, 1 / 365)
    v = max(v, 0.05)
    price = calculate_price(s, k, 0.02, t, v, cp)
    delta = calculate_delta(s, k, 0.02, t, v, cp)
    gamma = calculate_gamma(s, k, 0.02, t, v)
    vega = calculate_vega(s, k, 0.02, t, v)
    return price, delta, gamma, vega


def in_session(stamp: str) -> bool:
    hhmm = stamp[11:16]
    return ("09:30" <= hhmm <= "11:30") or ("13:00" <= hhmm <= "15:00")


def load_minutes() -> list[tuple[str, float, float]]:
    if not MINUTE_CACHE.exists() or MINUTE_CACHE.stat().st_size < 100:
        raise RuntimeError("缺少 30 分钟缓存，请先运行 fetch_csi300_30min.py")
    raw = json.loads(MINUTE_CACHE.read_text(encoding="utf-8"))
    bars: list[tuple[str, float, float]] = []
    for row in raw:
        stamp = str(row[0])
        if stamp.replace("-", "")[:8] < "20250819":
            continue
        if not in_session(stamp):
            continue
        close = float(row[4])
        volume = float(row[5] if len(row) > 5 else 0.0)
        if close <= 0:
            continue
        bars.append((stamp, close, volume))
    if len(bars) < 200:
        raise RuntimeError(f"沪深300 30分钟样本不足: {len(bars)}")
    return bars


@dataclass
class Params:
    name: str
    gamma: float = 0.08
    kappa: float = 1.4
    sigma_floor: float = 0.18
    tau_days: float = 0.15
    theo_weight: float = 0.65
    min_spread_ticks: int = 2
    vol_spread: float = 0.015
    max_pos: int = 10
    quote_volume: int = 1
    flatten: float = 0.75
    hedge: bool = True
    spread_mult: float = 0.02


PRESETS: list[Params] = [
    Params("实盘默认参数", gamma=0.08, kappa=1.4, hedge=True, spread_mult=0.02),
    Params("校准AS+对冲", gamma=0.08, kappa=1.4, hedge=True, spread_mult=0.003),
    Params("更厌恶库存", gamma=0.16, kappa=1.4, hedge=True, spread_mult=0.003),
    Params("更紧价差", gamma=0.04, kappa=2.2, hedge=True, spread_mult=0.003),
    Params("校准但不对冲", gamma=0.08, kappa=1.4, hedge=False, spread_mult=0.003),
    Params("固定2跳价差", gamma=0.001, kappa=8.0, min_spread_ticks=2, vol_spread=0.0, hedge=True, spread_mult=0.0001),
]


def preset_payloads() -> list[dict[str, Any]]:
    return [asdict(item) for item in PRESETS]


def cache_meta() -> dict[str, Any]:
    info: dict[str, Any] = {
        "exists": MINUTE_CACHE.exists(),
        "path": str(MINUTE_CACHE),
        "bars": 0,
        "days": 0,
        "start": "",
        "end": "",
        "raw_bars": 0,
    }
    if not MINUTE_CACHE.exists():
        return info
    info["size"] = MINUTE_CACHE.stat().st_size
    try:
        raw = json.loads(MINUTE_CACHE.read_text(encoding="utf-8"))
        info["raw_bars"] = len(raw)
        dates: list[str] = []
        for row in raw:
            stamp = str(row[0])
            if stamp.replace("-", "")[:8] < "20250819":
                continue
            if not in_session(stamp):
                continue
            close = float(row[4])
            if close <= 0:
                continue
            dates.append(stamp[:10])
        info["bars"] = len(dates)
        info["days"] = len(set(dates))
        info["start"] = dates[0] if dates else ""
        info["end"] = dates[-1] if dates else ""
    except Exception as exc:
        info["error"] = str(exc)
    return info


def load_saved_result() -> dict[str, Any] | None:
    if not RESULT_PATH.exists() or RESULT_PATH.stat().st_size < 20:
        return None
    try:
        data = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def daily_closes(bars: list[tuple[str, float, float]]) -> tuple[list[str], np.ndarray, np.ndarray]:
    dates: list[str] = []
    closes: list[float] = []
    bar_date_idx = np.empty(len(bars), dtype=np.int32)
    last = ""
    idx = -1
    for i, (stamp, close, _vol) in enumerate(bars):
        day = stamp[:10].replace("-", "")
        if day != last:
            dates.append(day)
            closes.append(close)
            last = day
            idx += 1
        else:
            closes[-1] = close
        bar_date_idx[i] = idx
    return dates, np.array(closes, dtype=float), bar_date_idx


def chart_keep(n: int, target: int = CHART_BINS) -> np.ndarray:
    if n <= 0:
        return np.zeros(0, dtype=np.int32)
    if n <= target:
        return np.arange(n, dtype=np.int32)
    return np.unique(np.round(np.linspace(0, n - 1, target)).astype(np.int32))


def take_series(values: list[float], keep: np.ndarray, digits: int = 4) -> list[float]:
    return [round(float(values[int(i)]), digits) for i in keep]


def pack_chart(
    stamps: list[str],
    spots: list[float],
    sigmas: list[float],
    calls: list[float],
    puts: list[float],
    nets: list[float],
    spreads: list[float],
    biases: list[float],
    deltas: list[float],
    futs: list[float],
    trades: list[dict[str, Any]],
) -> dict[str, Any]:
    keep = chart_keep(len(spots))
    if len(keep) == 0:
        return {}
    keep_list = [int(i) for i in keep]
    buckets: dict[tuple[int, int, str], int] = {}
    j = 0
    for trade in trades:
        idx = int(trade["i"])
        while j + 1 < len(keep_list) and keep_list[j + 1] <= idx:
            j += 1
        key = (j, int(trade["s"]), str(trade["k"]))
        buckets[key] = buckets.get(key, 0) + int(trade.get("n") or 1)
    packed_trades = [
        {"i": i, "s": side, "k": kind, "n": count}
        for (i, side, kind), count in sorted(buckets.items())
    ]
    return {
        "x": [stamps[i][2:16] for i in keep_list],
        "spot": take_series(spots, keep, 2),
        "sigma": take_series(sigmas, keep, 4),
        "call": take_series(calls, keep, 2),
        "put": take_series(puts, keep, 2),
        "net": take_series(nets, keep, 2),
        "spread": take_series(spreads, keep, 3),
        "bias": take_series(biases, keep, 3),
        "delta": take_series(deltas, keep, 2),
        "fut": take_series(futs, keep, 2),
        "trades": packed_trades,
    }


def fill_probability(dist_ticks: float, volume: float, med_volume: float, bars_per_day: float) -> float:
    """Scale the old 24-step fill odds to the actual bar frequency."""
    p0 = FILL_P0 * (DAILY_STEPS_REF / max(bars_per_day, 1.0))
    p = p0 * math.exp(-0.45 * dist_ticks)
    if med_volume > 0 and volume > 0:
        p *= min(3.0, max(0.35, volume / med_volume))
    return min(0.85, p)


def run_one(
    bars: list[tuple[str, float, float]],
    params: Params,
    rng: np.random.Generator,
    with_chart: bool = True,
) -> dict:
    dates, closes, bar_date_idx = daily_closes(bars)
    n_days = len(closes)
    logret = np.diff(np.log(closes), prepend=np.log(closes[0]))
    vols = np.array([row[2] for row in bars], dtype=float)
    med_volume = float(np.median(vols[vols > 0])) if np.any(vols > 0) else 1.0
    counts = np.bincount(bar_date_idx)
    bars_per_day = float(np.median(counts[counts > 0])) if len(counts) else 8.0

    call_pos = 0
    put_pos = 0
    fut_pos = 0.0
    cash = 0.0
    equity = []
    daily_pnl = []
    fills = 0
    buy_fills = 0
    sell_fills = 0
    spread_cap = []
    inv_series = []
    skip_risk = 0
    last_equity = 0.0
    peak = 0.0
    max_dd = 0.0
    turnover_opt = 0
    turnover_fut = 0
    last_day_idx = -1
    day_start = 0.0
    theo_c = theo_p = d_c = d_p = 0.0
    last_s = closes[0]
    curve_step = max(1, len(bars) // 50)
    curve_x: list[str] = []
    curve_y: list[float] = []
    chart_stamps: list[str] = []
    chart_spots: list[float] = []
    chart_sigmas: list[float] = []
    chart_calls: list[float] = []
    chart_puts: list[float] = []
    chart_nets: list[float] = []
    chart_spreads: list[float] = []
    chart_biases: list[float] = []
    chart_deltas: list[float] = []
    chart_futs: list[float] = []
    chart_trades: list[dict[str, Any]] = []

    sigmas = np.empty(n_days, dtype=float)
    for i in range(n_days):
        if i < 20:
            sigmas[i] = params.sigma_floor
        else:
            sigmas[i] = min(0.55, max(params.sigma_floor, float(np.std(logret[i - 20 : i]) * math.sqrt(242))))

    tte = 21 / 365.0

    for i, (stamp, s, volume) in enumerate(bars):
        day_idx = int(bar_date_idx[i])
        if day_idx != last_day_idx:
            if last_day_idx >= 0:
                equity.append(round(last_equity, 2))
                daily_pnl.append(round(last_equity - day_start, 2))
                inv_series.append(call_pos + put_pos)
                if abs(call_pos) >= params.max_pos or abs(put_pos) >= params.max_pos:
                    skip_risk += 1
            day_start = last_equity
            last_day_idx = day_idx

        sigma = float(sigmas[day_idx])
        k = max(STRIKE_STEP, round_to(s, STRIKE_STEP))
        theo_c, d_c, g_c, v_c = greeks(s, k, tte, sigma, 1)
        theo_p, d_p, g_p, v_p = greeks(s, k, tte, sigma, -1)
        noise = rng.normal(0, 0.0008)
        mkt_c = theo_c * (1 + noise)
        mkt_p = theo_p * (1 - noise * 0.5)
        half_mkt = PRICETICK * 2
        last_s = s
        call_spread = 0.0
        call_bias = 0.0

        for kind, theo, mkt, delta, vega, pos in (
            ("C", theo_c, mkt_c, d_c, v_c, call_pos),
            ("P", theo_p, mkt_p, d_p, v_p, put_pos),
        ):
            mid = params.theo_weight * theo + (1 - params.theo_weight) * mkt
            bid_mkt = max(PRICETICK, floor_to(mkt - half_mkt, PRICETICK))
            ask_mkt = ceil_to(mkt + half_mkt, PRICETICK)
            inv = pos / max(params.max_pos, 1)
            reservation, as_half = as_quotes(
                mid, inv, params.gamma, params.kappa, sigma, params.tau_days, params.spread_mult
            )
            vega_half = params.vol_spread * (abs(vega) / 100.0) / 2
            half = max(as_half, vega_half, params.min_spread_ticks * PRICETICK / 2)
            if kind == "C":
                call_spread = half * 2
                call_bias = reservation - mid
            bid = floor_to(reservation - half, PRICETICK)
            ask = ceil_to(reservation + half, PRICETICK)
            if ask_mkt:
                bid = min(bid, ask_mkt - PRICETICK)
            if bid_mkt:
                ask = max(ask, bid_mkt + PRICETICK)
            if bid <= 0 or ask <= bid:
                continue
            allow_bid = pos < params.max_pos
            allow_ask = pos > -params.max_pos
            if abs(inv) >= params.flatten and pos > 0:
                allow_bid = False
            if abs(inv) >= params.flatten and pos < 0:
                allow_ask = False
            vol = params.quote_volume

            filled_side = 0
            fill_px = 0.0
            if allow_bid and bid < ask_mkt:
                dist = max(0.0, (bid_mkt - bid) / PRICETICK)
                if bid >= ask_mkt - 1e-12:
                    filled_side = 1
                    fill_px = ask_mkt
                elif rng.random() < fill_probability(dist, volume, med_volume, bars_per_day):
                    filled_side = 1
                    fill_px = bid
            if filled_side == 0 and allow_ask and ask > bid_mkt:
                dist = max(0.0, (ask - ask_mkt) / PRICETICK)
                if ask <= bid_mkt + 1e-12:
                    filled_side = -1
                    fill_px = bid_mkt
                elif rng.random() < fill_probability(dist, volume, med_volume, bars_per_day):
                    filled_side = -1
                    fill_px = ask
            if filled_side:
                notional = fill_px * OPT_SIZE * vol * filled_side
                cash -= notional
                cash -= OPT_COMM * vol
                if kind == "C":
                    call_pos += filled_side * vol
                else:
                    put_pos += filled_side * vol
                fills += 1
                turnover_opt += vol
                if filled_side > 0:
                    buy_fills += 1
                else:
                    sell_fills += 1
                spread_cap.append((mid - fill_px) * filled_side * OPT_SIZE)
                if with_chart:
                    chart_trades.append({"i": i, "s": filled_side, "k": kind, "n": vol})

        pos_delta = call_pos * d_c * OPT_SIZE + put_pos * d_p * OPT_SIZE
        if params.hedge:
            target = -pos_delta / FUT_SIZE
            diff = target - fut_pos
            if abs(diff) >= 1:
                lots = int(round(diff))
                cash -= lots * s * FUT_SIZE
                cash -= abs(lots) * FUT_COMM
                fut_pos += lots
                turnover_fut += abs(lots)
                if with_chart:
                    chart_trades.append({"i": i, "s": 1 if lots > 0 else -1, "k": "F", "n": abs(lots)})

        last_equity = (
            call_pos * theo_c * OPT_SIZE
            + put_pos * theo_p * OPT_SIZE
            + fut_pos * s * FUT_SIZE
            + cash
        )
        peak = max(peak, last_equity)
        max_dd = min(max_dd, last_equity - peak)
        if i % curve_step == 0 or i == len(bars) - 1:
            curve_x.append(stamp[5:16])
            curve_y.append(round(last_equity, 1))
        if with_chart:
            chart_stamps.append(stamp)
            chart_spots.append(s)
            chart_sigmas.append(sigma)
            chart_calls.append(float(call_pos))
            chart_puts.append(float(put_pos))
            chart_nets.append(float(call_pos + put_pos))
            chart_spreads.append(call_spread)
            chart_biases.append(call_bias)
            chart_deltas.append(pos_delta)
            chart_futs.append(float(fut_pos))

    if last_day_idx >= 0:
        equity.append(round(last_equity, 2))
        daily_pnl.append(round(last_equity - day_start, 2))
        inv_series.append(call_pos + put_pos)

    flatten_cost = (abs(call_pos) + abs(put_pos)) * OPT_COMM + abs(fut_pos) * FUT_COMM
    final = last_equity - flatten_cost

    eq = np.array(equity, dtype=float)
    pnl = np.array(daily_pnl, dtype=float)
    rets = pnl / max(200000, 1)
    sharpe = float(np.mean(rets) / (np.std(rets) + 1e-9) * math.sqrt(242)) if len(rets) > 2 else 0.0
    win = float(np.mean(pnl > 0)) if len(pnl) else 0.0
    months: dict[str, float] = {}
    for date, p in zip(dates, daily_pnl):
        key = date[:6]
        months[key] = round(months.get(key, 0.0) + p, 2)

    step = max(1, len(eq) // 40)
    eq_x = [dates[i][4:] for i in range(0, len(dates), step)]
    eq_y = [round(float(eq[i]), 1) for i in range(0, len(eq), step)]
    if dates and eq_x[-1] != dates[-1][4:]:
        eq_x.append(dates[-1][4:])
        eq_y.append(round(float(eq[-1]), 1))

    return {
        "name": params.name,
        "start": dates[0] if dates else "",
        "end": dates[-1] if dates else "",
        "days": n_days,
        "bars": len(bars),
        "bars_per_day": round(bars_per_day, 1),
        "final_pnl": round(final, 2),
        "sharpe": round(sharpe, 3),
        "max_dd": round(max_dd, 2),
        "win_rate": round(win * 100, 1),
        "fills": fills,
        "buy_fills": buy_fills,
        "sell_fills": sell_fills,
        "avg_spread_capture": round(float(np.mean(spread_cap)), 2) if spread_cap else 0.0,
        "opt_turnover": turnover_opt,
        "fut_turnover": turnover_fut,
        "avg_net_opt": round(float(np.mean(np.abs(inv_series))), 2) if inv_series else 0.0,
        "end_call": call_pos,
        "end_put": put_pos,
        "end_fut": fut_pos,
        "end_spot": last_s,
        "risk_hits": skip_risk,
        "monthly": months,
        "equity_x": eq_x,
        "equity_y": eq_y,
        "curve_x": curve_x,
        "curve_y": curve_y,
        "daily_mean": round(float(np.mean(pnl)), 2) if len(pnl) else 0.0,
        "daily_std": round(float(np.std(pnl)), 2) if len(pnl) else 0.0,
        "best_day": round(float(np.max(pnl)), 2) if len(pnl) else 0,
        "worst_day": round(float(np.min(pnl)), 2) if len(pnl) else 0,
        "hedge": params.hedge,
        "gamma": params.gamma,
        "kappa": params.kappa,
        "spread_mult": params.spread_mult,
        "tau_days": params.tau_days,
        "sigma_floor": params.sigma_floor,
        "max_pos": params.max_pos,
        "chart": pack_chart(
            chart_stamps,
            chart_spots,
            chart_sigmas,
            chart_calls,
            chart_puts,
            chart_nets,
            chart_spreads,
            chart_biases,
            chart_deltas,
            chart_futs,
            chart_trades,
        ) if with_chart else {},
    }


def params_from_dict(data: dict[str, Any] | None) -> Params:
    payload = dict(data or {})
    name = str(payload.pop("name", "") or "自定义参数")
    known = {key: value for key, value in payload.items() if key in Params.__dataclass_fields__ and key != "name"}
    return Params(name, **known)


def grid_axis(start: float, end: float, step: float, label: str) -> list[float]:
    start_v = float(start)
    end_v = float(end)
    step_v = float(step)
    if step_v <= 0:
        raise ValueError(f"{label} 步长必须大于 0")
    if end_v < start_v:
        start_v, end_v = end_v, start_v
    count = int(math.floor((end_v - start_v) / step_v + 1e-9)) + 1
    if count > 40:
        raise ValueError(f"{label} 网格超过 40 档，请加大步长")
    values = [round(start_v + i * step_v, 8) for i in range(max(count, 1))]
    return values or [round(start_v, 8)]


def score_row(row: dict[str, Any], objective: str) -> float:
    pnl = float(row.get("final_pnl") or 0)
    sharpe = float(row.get("sharpe") or 0)
    drawdown = abs(float(row.get("max_dd") or 0))
    if objective == "pnl":
        return pnl
    if objective == "calmar":
        return pnl / max(drawdown, 1.0)
    return sharpe


def combo_label(params: Params, rank: int | None = None) -> str:
    prefix = f"#{rank} " if rank else ""
    hedge = "对冲" if params.hedge else "无对冲"
    return (
        f"{prefix}γ={params.gamma:g} κ={params.kappa:g} "
        f"价差={params.spread_mult:g} τ={params.tau_days:g} {hedge}"
    )


def backtest_payload(
    results: list[dict[str, Any]],
    bars: list[tuple[str, float, float]],
    seed: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dates = [stamp[:10] for stamp, _s, _v in bars]
    out = {
        "generated": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "universe": "沪深300指数30分钟线 IO ATM Call/Put + IF 对冲",
        "interval": "30min",
        "compare": False,
        "assumptions": {
            "option_size": OPT_SIZE,
            "futures_size": FUT_SIZE,
            "pricetick": PRICETICK,
            "opt_commission": OPT_COMM,
            "fut_commission": FUT_COMM,
            "dte_days": 21,
            "capital_for_sharpe": 200000,
            "fill_p0_unscaled": FILL_P0,
            "fill_scale_ref_steps": DAILY_STEPS_REF,
            "source": "akshare sh000300 period=30",
            "seed": seed,
        },
        "sample": {
            "start": dates[0] if dates else "",
            "end": dates[-1] if dates else "",
            "bars": len(bars),
            "days": len(set(dates)),
        },
        "results": results,
    }
    if extra:
        out.update(extra)
    return out


def run_backtest(
    params: Params | None = None,
    compare: bool = False,
    seed: int = SEED,
) -> dict[str, Any]:
    bars = load_minutes()
    variants = list(PRESETS) if compare else [params or PRESETS[0]]
    results = []
    for item in variants:
        rng = np.random.default_rng(seed)
        results.append(run_one(bars, item, rng))
    out = backtest_payload(results, bars, seed, {"compare": compare})
    RESULT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def run_optimize(
    base: Params,
    gamma: tuple[float, float, float],
    kappa: tuple[float, float, float],
    spread_mult: tuple[float, float, float],
    tau_days: tuple[float, float, float],
    hedge_mode: str = "on",
    objective: str = "sharpe",
    seed: int = SEED,
    on_progress: Any = None,
) -> dict[str, Any]:
    objective = objective if objective in {"sharpe", "pnl", "calmar"} else "sharpe"
    if hedge_mode == "off":
        hedges = [False]
    elif hedge_mode == "both":
        hedges = [True, False]
    else:
        hedges = [True]
    axes = {
        "gamma": grid_axis(*gamma, "γ"),
        "kappa": grid_axis(*kappa, "κ"),
        "spread_mult": grid_axis(*spread_mult, "价差乘数"),
        "tau_days": grid_axis(*tau_days, "视野"),
    }
    combos = [
        Params(
            name="",
            gamma=g,
            kappa=k,
            spread_mult=sm,
            tau_days=td,
            hedge=h,
            sigma_floor=base.sigma_floor,
            theo_weight=base.theo_weight,
            min_spread_ticks=base.min_spread_ticks,
            vol_spread=base.vol_spread,
            max_pos=base.max_pos,
            quote_volume=base.quote_volume,
            flatten=base.flatten,
        )
        for g, k, sm, td, h in product(
            axes["gamma"],
            axes["kappa"],
            axes["spread_mult"],
            axes["tau_days"],
            hedges,
        )
    ]
    if len(combos) > MAX_COMBOS:
        raise ValueError(f"组合 {len(combos)} 组超过上限 {MAX_COMBOS}，请加大步长")
    if not combos:
        raise ValueError("寻优网格为空")

    bars = load_minutes()
    results: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    total = len(combos)
    labels = {"sharpe": "夏普", "pnl": "盈亏", "calmar": "卡玛"}

    def emit(done: int, message: str, rows: list[dict[str, Any]]) -> None:
        if not on_progress:
            return
        on_progress(
            {
                "done": done,
                "total": total,
                "message": message,
                "best": best,
                "result": backtest_payload(
                    rows,
                    bars,
                    seed,
                    {
                        "optimize": {
                            "objective": objective,
                            "objective_label": labels[objective],
                            "combos": total,
                            "axes": axes,
                            "hedge_mode": hedge_mode,
                            "partial": done < total,
                        }
                    },
                ),
            }
        )

    for index, item in enumerate(combos, 1):
        item.name = combo_label(item)
        row = run_one(bars, item, np.random.default_rng(seed), with_chart=False)
        row["name"] = item.name
        row["score"] = round(score_row(row, objective), 4)
        results.append(row)
        if best is None or row["score"] > float(best.get("score") or -1e18):
            best = row
        ranked = sorted(results, key=lambda item_row: float(item_row.get("score") or 0), reverse=True)
        emit(index, f"寻优 {index}/{total} 最佳{labels[objective]} {best['score'] if best else '—'}", ranked)

    ranked = sorted(results, key=lambda item_row: float(item_row.get("score") or 0), reverse=True)
    for rank, row in enumerate(ranked, 1):
        row["rank"] = rank
        row["name"] = combo_label(params_from_dict(row), rank)

    emit(total, f"正在生成前 {min(CHART_TOP, len(ranked))} 名成交图…", ranked)
    for row in ranked[:CHART_TOP]:
        item = params_from_dict(row)
        item.name = row["name"]
        full = run_one(bars, item, np.random.default_rng(seed), with_chart=True)
        row["chart"] = full.get("chart") or {}
        row["equity_x"] = full.get("equity_x") or row.get("equity_x")
        row["equity_y"] = full.get("equity_y") or row.get("equity_y")

    out = backtest_payload(
        ranked,
        bars,
        seed,
        {
            "optimize": {
                "objective": objective,
                "objective_label": labels[objective],
                "combos": total,
                "axes": axes,
                "hedge_mode": hedge_mode,
                "partial": False,
                "best_name": ranked[0]["name"] if ranked else "",
                "best_score": ranked[0]["score"] if ranked else None,
            }
        },
    )
    RESULT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> None:
    out = run_backtest(compare=True)
    print(
        json.dumps(
            {
                row["name"]: {
                    "pnl": row["final_pnl"],
                    "sharpe": row["sharpe"],
                    "dd": row["max_dd"],
                    "fills": row["fills"],
                    "days": row["days"],
                    "bars": row["bars"],
                }
                for row in out["results"]
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print("saved", RESULT_PATH)


if __name__ == "__main__":
    main()
