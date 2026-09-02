"""Backtest IF 5-minute high-frequency mean-reversion.

Default preset matches the walk-forward winner:
look=16, z=2.4, hold=6, stop=1.5ATR, tp=1.0ATR, risk=0.6%, AM-only 09:45-11:25.
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

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from if_hf_mr import bar_atr, return_zscore, target_lots  # noqa: E402

CACHE = ROOT.joinpath("if_5min_cache.json")
RESULT_PATH = ROOT.joinpath("if_hf_mr_if_5m_report.json")
SIZE = 300.0
COMM = 25.0
SLIP = 0.2  # points per side
CAPITAL = 1_000_000.0


@dataclass
class Params:
    name: str
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


PRESETS: list[Params] = [
    Params("推荐-上午MR"),
    Params("稳健低仓", risk=0.004, max_lots=3),
    Params("稍积极", risk=0.008, max_lots=5),
    Params("全日时段", session_end="14:15", force_flat="14:55"),
    Params("更高阈值", z_entry=2.5),
]


def load_bars() -> list[dict[str, Any]]:
    if not CACHE.exists() or CACHE.stat().st_size < 100:
        raise RuntimeError("缺少 IF 5 分钟缓存，请先运行 fetch_if_5min.py")
    raw = json.loads(CACHE.read_text(encoding="utf-8"))
    out: list[dict[str, Any]] = []
    for row in raw:
        close = float(row[4])
        if close <= 0:
            continue
        stamp = str(row[0])
        out.append(
            {
                "datetime": stamp,
                "date": stamp[:10],
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": close,
                "volume": float(row[5] or 0),
            }
        )
    if len(out) < 500:
        raise RuntimeError(f"IF 5 分钟样本不足: {len(out)}")
    return out


def side_cost(lots: int) -> float:
    return abs(lots) * (COMM + SLIP * SIZE)


def calendar_years(start: str, end: str) -> float:
    a = datetime.strptime(start[:10], "%Y-%m-%d")
    b = datetime.strptime(end[:10], "%Y-%m-%d")
    return max((b - a).days / 365.25, 1e-9)


def run_one(bars: list[dict[str, Any]], params: Params) -> dict[str, Any]:
    cash = 0.0
    pos = 0
    last = float(bars[0]["close"])
    day_nav: dict[str, float] = {}
    trades: list[dict[str, Any]] = []
    opens = stops = tps = time_exits = 0
    exit_i = -1
    entry_px = stop_dist = tp_dist = 0.0
    day = ""
    day_trades = 0
    hist: list[dict[str, float]] = []
    closes: list[float] = []
    peak = CAPITAL
    max_dd = 0.0
    max_dd_peak_pct = 0.0

    for i, row in enumerate(bars):
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        d, stamp = row["date"], row["datetime"]
        hhmm = stamp[11:16]
        if d != day:
            if pos:
                cash -= side_cost(pos)
                trades.append({"date": stamp, "action": "换日平仓", "lots": pos, "price": round(last, 1)})
                pos = 0
            day = d
            day_trades = 0
        if pos:
            cash += pos * (o - last) * SIZE
        last = o

        if pos:
            reason = None
            px = 0.0
            if pos > 0:
                if l <= entry_px - stop_dist:
                    px = min(o, entry_px - stop_dist)
                    reason = "止损"
                elif tp_dist > 0 and h >= entry_px + tp_dist:
                    px = max(o, entry_px + tp_dist)
                    reason = "止盈"
            else:
                if h >= entry_px + stop_dist:
                    px = max(o, entry_px + stop_dist)
                    reason = "止损"
                elif tp_dist > 0 and l <= entry_px - tp_dist:
                    px = min(o, entry_px - tp_dist)
                    reason = "止盈"
            if reason is None and (i >= exit_i or hhmm >= params.force_flat):
                px = c
                reason = "到期"
            if reason:
                cash += pos * (px - last) * SIZE
                last = px
                cash -= side_cost(pos)
                trades.append({"date": stamp, "action": reason, "lots": pos, "price": round(px, 1)})
                if reason == "止损":
                    stops += 1
                elif reason == "止盈":
                    tps += 1
                else:
                    time_exits += 1
                pos = 0

        if pos:
            cash += pos * (c - last) * SIZE
        last = c
        hist.append({"high": h, "low": l, "close": c})
        if len(hist) > 80:
            hist = hist[-80:]
        closes.append(c)
        if len(closes) > 120:
            closes = closes[-120:]
        nav = CAPITAL + cash
        day_nav[d] = nav
        peak = max(peak, nav)
        max_dd = min(max_dd, nav - peak)
        if peak > 0:
            max_dd_peak_pct = min(max_dd_peak_pct, (nav - peak) / peak)

        if pos or day_trades >= params.max_day_trades:
            continue
        if not (params.session_start <= hhmm <= params.session_end):
            continue
        if len(closes) < params.look + 2:
            continue
        z, rvol = return_zscore(closes, params.look)
        if abs(z) < params.z_entry:
            continue
        if not (params.vol_min <= rvol <= params.vol_max):
            continue
        atr = bar_atr(hist, params.atr_n)
        stop_dist = max(params.stop_atr * atr, 1.5)
        tp_dist = max(params.tp_atr * atr, 0.0) if params.tp_atr else 0.0
        lots = target_lots(nav, stop_dist, SIZE, params.risk, params.max_lots, min_lots=1)
        if lots < 1:
            continue
        side = -1 if z > 0 else 1
        pos = side * lots
        entry_px = c
        exit_i = i + params.hold_bars
        cash -= side_cost(pos)
        day_trades += 1
        opens += 1
        trades.append(
            {
                "date": stamp,
                "action": "开多" if side > 0 else "开空",
                "lots": pos,
                "price": round(c, 1),
                "z": round(z, 3),
                "rvol": round(rvol, 3),
                "atr": round(atr, 2),
            }
        )

    if pos:
        cash -= side_cost(pos)
        pos = 0

    keys = sorted(day_nav)
    eq = np.array([day_nav[k] for k in keys], dtype=float)
    pnl = np.r_[eq[0] - CAPITAL, np.diff(eq)]
    rets = pnl / np.maximum(np.r_[CAPITAL, eq[:-1]], 1.0)
    sharpe = float(np.mean(rets) / (np.std(rets) + 1e-9) * math.sqrt(242)) if len(rets) > 2 else 0.0
    years = calendar_years(keys[0], keys[-1])
    final_nav = float(eq[-1])
    cagr = (max(final_nav, 1e-9) / CAPITAL) ** (1.0 / years) - 1.0
    calmar = (cagr * 100.0) / abs(max_dd_peak_pct * 100.0) if abs(max_dd_peak_pct) > 1e-9 else 0.0
    months: dict[str, float] = {}
    yearly: dict[str, float] = {}
    for day_key, value in zip(keys, pnl):
        months[day_key[:7]] = round(months.get(day_key[:7], 0.0) + float(value), 2)
        yearly[day_key[:4]] = round(yearly.get(day_key[:4], 0.0) + float(value), 2)
    pos_years = sum(1 for value in yearly.values() if value > 0)
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
        "max_dd": round(float(max_dd), 2),
        "max_dd_peak_pct": round(100.0 * max_dd_peak_pct, 2),
        "opens": opens,
        "stops": stops,
        "take_profits": tps,
        "time_exits": time_exits,
        "pos_year_pct": round(100.0 * pos_years / max(len(yearly), 1), 1),
        "monthly": months,
        "yearly": yearly,
        "equity_x": [keys[i][2:7] for i in range(0, len(keys), step)],
        "equity_y": [round(float(eq[i]) - CAPITAL, 1) for i in range(0, len(keys), step)],
        "trades": trades[-50:],
        "daily_mean": round(float(np.mean(pnl)), 2),
        "daily_std": round(float(np.std(pnl)), 2),
        "best_day": round(float(np.max(pnl)), 2),
        "worst_day": round(float(np.min(pnl)), 2),
        **asdict(params),
    }


def split_metrics(bars: list[dict[str, Any]], params: Params) -> dict[str, Any]:
    is_bars = [b for b in bars if b["date"] <= "2023-12-31"]
    oos_bars = [b for b in bars if b["date"] >= "2024-01-01"]
    return {
        "in_sample": {k: run_one(is_bars, params)[k] for k in ("cagr", "sharpe", "max_dd_peak_pct", "opens", "final_pnl")},
        "out_of_sample": {k: run_one(oos_bars, params)[k] for k in ("cagr", "sharpe", "max_dd_peak_pct", "opens", "final_pnl")},
    }


def preset_payloads() -> list[dict[str, Any]]:
    return [asdict(item) for item in PRESETS]


def run_backtest(compare: bool = True) -> dict[str, Any]:
    bars = load_bars()
    variants = list(PRESETS) if compare else PRESETS[:1]
    results = [run_one(bars, item) for item in variants]
    lead = results[0]
    out = {
        "generated": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "engine": "if_hf_mr",
        "kind": "IF",
        "universe": "中金所IF 5分钟 → 高频均值回归",
        "interval": "5m",
        "capital": CAPITAL,
        "assumptions": {
            "futures_size": SIZE,
            "commission": COMM,
            "slippage_points": SLIP,
            "structure": "5m return z-score fade, AM-only, 1 trade/day",
            "entry": f"z≥{lead['z_entry']}, look={lead['look']}, rvol∈[{lead['vol_min']},{lead['vol_max']}]",
            "exit": f"stop={lead['stop_atr']}ATR, tp={lead['tp_atr']}ATR, hold≤{lead['hold_bars']} bars, flat@{lead['force_flat']}",
            "session": f"{lead['session_start']}-{lead['session_end']}",
            "risk": f"单笔风险≤{100 * lead['risk']:.1f}%净值",
            "source": "新浪分月IF 5分钟拼接（fetch_if_5min.py）",
        },
        "sample": {
            "start": bars[0]["datetime"],
            "end": bars[-1]["datetime"],
            "bars": len(bars),
            "days": len({row["date"] for row in bars}),
        },
        "walk_forward": split_metrics(bars, PRESETS[0]),
        "recommend": results[0]["name"],
        "results": results,
    }
    RESULT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> None:
    out = run_backtest(compare=True)
    print(f"{out['sample']['start']} → {out['sample']['end']}  bars={out['sample']['bars']}")
    print("walk_forward", out["walk_forward"])
    for row in out["results"]:
        print(
            f"{row['name']}: CAGR={row['cagr']:.2f}% Sharpe={row['sharpe']:.2f} "
            f"DD={row['max_dd_peak_pct']:.2f}% 开仓={row['opens']} "
            f"止盈={row['take_profits']} 止损={row['stops']} 到期={row['time_exits']}"
        )
    print(f"结果写入 {RESULT_PATH}")


if __name__ == "__main__":
    main()
