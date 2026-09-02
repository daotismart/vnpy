"""Backtest SA CTA on 30-minute bars: 20-day Donchian, ATR trail, vol targeting."""

from __future__ import annotations

import json
import math
import sys
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sa_cta_trend import realized_vol, target_lots, true_range  # noqa: E402

CACHE_30MIN = ROOT.joinpath("sa_30min_cache.json")
RESULT_PATH = ROOT.joinpath("backtest_sa_cta_trend_result.json")
SIZE = 20.0
COMM = 3.0
SLIP = 1.0
CAPITAL = 1_000_000.0
GAP_HOURS = 72.0


def parse_dt(text: str) -> datetime:
    stamp = str(text).replace("T", " ")[:19]
    if len(stamp) == 16:
        stamp += ":00"
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")


def load_bars() -> list[dict[str, Any]]:
    if not CACHE_30MIN.exists() or CACHE_30MIN.stat().st_size < 100:
        raise RuntimeError("缺少 SA 30 分钟缓存，请先运行 fetch_sa_30min.py")
    raw = json.loads(CACHE_30MIN.read_text(encoding="utf-8"))
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
            }
        )
    if len(out) < 80:
        raise RuntimeError(f"SA 30 分钟样本不足: {len(out)}")
    return out


@dataclass
class Params:
    name: str
    entry_days: int = 20
    exit_days: int = 10
    atr_n: int = 20
    atr_stop: float = 2.5
    risk: float = 0.012
    vol_tgt: float = 0.12
    max_lots: int = 80
    bar_entry: int = 0
    bar_exit: int = 0
    close_only: bool = False
    intraday_stop: bool = True


PRESETS: list[Params] = [
    Params("盘中突破+日终止损", close_only=False, intraday_stop=False),
    Params("盘中突破止损"),
    Params("收盘确认", close_only=True),
    Params("买持有", risk=0.0),
]


def fill_stop(side: int, level: float, o: float, h: float, l: float) -> float | None:
    if side > 0 and h >= level:
        return max(o, level)
    if side < 0 and l <= level:
        return min(o, level)
    return None


def calendar_years(start: str, end: str) -> float:
    a = datetime.strptime(start[:10], "%Y-%m-%d")
    b = datetime.strptime(end[:10], "%Y-%m-%d")
    return max((b - a).days / 365.25, 1e-9)


def run_one(bars: list[dict[str, Any]], params: Params) -> dict[str, Any]:
    n = len(bars)
    cash = 0.0
    pos = 0
    last_px = float(bars[0]["close"])
    extreme = 0.0
    equity: list[float] = []
    trades: list[dict[str, Any]] = []
    peak = CAPITAL
    max_dd = 0.0
    max_dd_peak_pct = 0.0
    day_highs: deque[float] = deque(maxlen=80)
    day_lows: deque[float] = deque(maxlen=80)
    day_closes: deque[float] = deque(maxlen=80)
    day_atr = 0.0
    session_high = 0.0
    session_low = 0.0
    session_close = 0.0
    prev_day = ""
    prev_dt: datetime | None = None
    bar_highs: deque[float] = deque(maxlen=120)
    bar_lows: deque[float] = deque(maxlen=120)

    def mark(px: float) -> None:
        nonlocal cash, last_px
        if pos != 0:
            cash += pos * (px - last_px) * SIZE
        last_px = px

    def flatten(px: float, reason: str, stamp: str) -> None:
        nonlocal cash, pos, extreme
        if pos == 0:
            return
        mark(px)
        cash -= abs(pos) * (COMM + SLIP * SIZE)
        trades.append({"date": stamp, "action": reason, "lots": pos, "price": round(px, 1)})
        pos = 0
        extreme = 0.0

    def open_pos(side: int, px: float, lots: int, stamp: str) -> None:
        nonlocal cash, pos, extreme
        pos = side * lots
        mark(px)
        cash -= abs(pos) * (COMM + SLIP * SIZE)
        extreme = px
        trades.append({"date": stamp, "action": "开多" if side > 0 else "开空", "lots": pos, "price": round(px, 1)})

    def roll_day() -> None:
        nonlocal day_atr, session_high, session_low, session_close
        if not prev_day or session_high <= 0:
            return
        prev_c = day_closes[-1] if day_closes else session_close
        day_highs.append(session_high)
        day_lows.append(session_low)
        day_closes.append(session_close)
        tr = true_range(session_high, session_low, prev_c)
        if len(day_highs) <= params.atr_n:
            day_atr = (day_atr * (len(day_highs) - 1) + tr) / max(len(day_highs), 1)
        else:
            day_atr = (day_atr * (params.atr_n - 1) + tr) / params.atr_n

    for i, row in enumerate(bars):
        stamp = row["datetime"]
        day = row["date"]
        oi, hi, lo, cl = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        now = parse_dt(stamp)
        if prev_dt is not None and (now - prev_dt).total_seconds() > GAP_HOURS * 3600:
            flatten(last_px, "断档平仓", stamp)
        if day != prev_day:
            roll_day()
            session_high, session_low, session_close = hi, lo, cl
            prev_day = day
        else:
            session_high = max(session_high, hi)
            session_low = min(session_low, lo) if session_low else lo
            session_close = cl

        mark(oi)
        nav = CAPITAL + cash
        ready = len(day_highs) >= max(params.entry_days, params.atr_n)

        if params.name == "买持有":
            if ready and pos == 0:
                hold_lots = max(1, int(0.10 * CAPITAL / max(oi * SIZE * 0.10, 1.0)))
                open_pos(1, oi, min(hold_lots, 80), stamp)
            mark(cl)
            nav = CAPITAL + cash
            peak = max(peak, nav)
            max_dd = min(max_dd, nav - peak)
            if peak > 0:
                max_dd_peak_pct = min(max_dd_peak_pct, (nav - peak) / peak)
            equity.append(nav)
            prev_dt = now
            continue

        use_bar = params.bar_entry > 0 and len(bar_highs) >= params.bar_entry
        hhmm = stamp[11:16] if len(stamp) >= 16 else ""
        session_end = (
            i + 1 >= n
            or bars[i + 1]["date"] != day
            or hhmm == "15:00"
            or ("14:55" <= hhmm <= "15:10")
            or hhmm >= "22:30"
        )
        allow_exit = params.intraday_stop or session_end
        if ready and pos != 0:
            if pos > 0:
                extreme = max(extreme, hi)
            else:
                extreme = min(extreme, lo)
        if ready and pos != 0 and allow_exit:
            atr = max(day_atr, 1e-6)
            stop = extreme - params.atr_stop * atr if pos > 0 else extreme + params.atr_stop * atr
            if use_bar:
                ch_lv = min(list(bar_lows)[-params.bar_exit :]) if pos > 0 else max(list(bar_highs)[-params.bar_exit :])
            else:
                ch_lv = min(list(day_lows)[-params.exit_days :]) if pos > 0 else max(list(day_highs)[-params.exit_days :])
            if params.close_only:
                stop_px = cl if (pos > 0 and cl <= stop) or (pos < 0 and cl >= stop) else None
                ch_px = cl if (pos > 0 and cl < ch_lv) or (pos < 0 and cl > ch_lv) else None
            else:
                stop_px = fill_stop(-int(np.sign(pos)), stop, oi, hi, lo)
                ch_px = fill_stop(-int(np.sign(pos)), float(ch_lv), oi, hi, lo)
            exit_px = stop_px
            if ch_px is not None and (
                exit_px is None or (pos > 0 and ch_px > exit_px) or (pos < 0 and ch_px < exit_px)
            ):
                exit_px = ch_px
            if exit_px is not None:
                flatten(exit_px, "止损" if stop_px == exit_px else "通道平仓", stamp)
                nav = CAPITAL + cash

        if ready:
            rvol = realized_vol(list(day_closes), 20)
            lots = target_lots(nav, max(day_atr, 1.0), SIZE, params.risk, params.vol_tgt, rvol, params.max_lots)
            if use_bar:
                up = max(list(bar_highs)[-params.bar_entry :])
                dn = min(list(bar_lows)[-params.bar_entry :])
            else:
                up = max(list(day_highs)[-params.entry_days :])
                dn = min(list(day_lows)[-params.entry_days :])
            if params.close_only:
                long_px = cl if cl > up else None
                short_px = cl if cl < dn else None
            else:
                long_px = fill_stop(1, up, oi, hi, lo)
                short_px = fill_stop(-1, dn, oi, hi, lo)
            if pos <= 0 and long_px is not None and lots >= 1:
                if pos < 0:
                    flatten(long_px, "反手", stamp)
                    nav = CAPITAL + cash
                    lots = target_lots(nav, max(day_atr, 1.0), SIZE, params.risk, params.vol_tgt, rvol, params.max_lots)
                if lots >= 1:
                    open_pos(1, long_px, lots, stamp)
            elif pos >= 0 and short_px is not None and lots >= 1:
                if pos > 0:
                    flatten(short_px, "反手", stamp)
                    nav = CAPITAL + cash
                    lots = target_lots(nav, max(day_atr, 1.0), SIZE, params.risk, params.vol_tgt, rvol, params.max_lots)
                if lots >= 1:
                    open_pos(-1, short_px, lots, stamp)

        mark(cl)
        bar_highs.append(hi)
        bar_lows.append(lo)
        nav = CAPITAL + cash
        peak = max(peak, nav)
        max_dd = min(max_dd, nav - peak)
        if peak > 0:
            max_dd_peak_pct = min(max_dd_peak_pct, (nav - peak) / peak)
        equity.append(nav)
        prev_dt = now

    if pos != 0:
        flatten(float(bars[-1]["close"]), "期末", bars[-1]["datetime"])
        equity[-1] = CAPITAL + cash

    eq = np.array(equity, dtype=float)
    day_nav: dict[str, float] = {}
    for row, nav in zip(bars, eq):
        day_nav[row["date"]] = float(nav)
    day_list = [day_nav[k] for k in sorted(day_nav)]
    dnav = np.array(day_list, dtype=float)
    dpnl = np.r_[dnav[0] - CAPITAL, np.diff(dnav)]
    prev = np.concatenate(([CAPITAL], dnav[:-1]))
    drets = dpnl / np.maximum(prev, 1.0)
    sharpe = float(np.mean(drets) / (np.std(drets) + 1e-9) * math.sqrt(242)) if len(drets) > 2 else 0.0
    years = calendar_years(bars[0]["date"], bars[-1]["date"])
    final_nav = float(eq[-1])
    cagr = (max(final_nav, 1e-9) / CAPITAL) ** (1.0 / years) - 1.0
    months: dict[str, float] = {}
    yearly: dict[str, float] = {}
    prev_nav = CAPITAL
    for day in sorted(day_nav):
        nav = day_nav[day]
        pnl = nav - prev_nav
        months[day[:7]] = round(months.get(day[:7], 0.0) + pnl, 2)
        yearly[day[:4]] = round(yearly.get(day[:4], 0.0) + pnl, 2)
        prev_nav = nav
    pos_months = sum(1 for v in months.values() if v > 0)
    step = max(1, len(dnav) // 48)
    keys = sorted(day_nav)
    eq_x = [keys[i][2:7] for i in range(0, len(keys), step)]
    eq_y = [round(day_nav[keys[i]] - CAPITAL, 1) for i in range(0, len(keys), step)]
    if eq_x[-1] != keys[-1][2:7]:
        eq_x.append(keys[-1][2:7])
        eq_y.append(round(day_nav[keys[-1]] - CAPITAL, 1))
    opens = [t for t in trades if t["action"] in ("开多", "开空")]
    unique_days = len(day_nav)
    return {
        "name": params.name,
        "start": bars[0]["datetime"],
        "end": bars[-1]["datetime"],
        "bars": n,
        "days": unique_days,
        "years": round(years, 3),
        "cagr": round(cagr * 100, 2),
        "final_pnl": round(final_nav - CAPITAL, 2),
        "final_nav": round(final_nav, 2),
        "sharpe": round(sharpe, 3),
        "max_dd": round(float(max_dd), 2),
        "max_dd_peak_pct": round(100.0 * max_dd_peak_pct, 2),
        "opens": len(opens),
        "trades": len(trades),
        "pos_month_pct": round(100.0 * pos_months / max(len(months), 1), 1),
        "monthly": months,
        "yearly": yearly,
        "equity_x": eq_x,
        "equity_y": eq_y,
        "best_day": round(float(np.max(dpnl)), 2),
        "worst_day": round(float(np.min(dpnl)), 2),
        **asdict(params),
    }


def run_backtest() -> dict[str, Any]:
    bars = load_bars()
    results = [run_one(bars, item) for item in PRESETS]
    out = {
        "generated": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "universe": "郑商所SA 30分钟 CTA（20日唐奇安 + ATR跟踪 + 波动率目标）",
        "interval": "30m",
        "capital": CAPITAL,
        "assumptions": {
            "futures_size": SIZE,
            "commission": COMM,
            "slippage": f"{SLIP} 元/吨 每边",
            "entry": "前20个已完成交易日最高/最低；30分钟K线触及止损价成交，或收盘价确认（close_only）",
            "exit": "前10日反向通道或 2.5×日ATR；盘中先触先平，或仅日终检查（15:00 / 22:30）；断档>72小时先平仓",
            "sizing": "单笔风险 1.2%净值/1日ATR，再按20日实现波动缩放到约12%年化",
            "source": "同一份 SA 5分钟缓存重采样为30分钟（与5分钟回测同一样本）",
            "note": "非严格主力连续，换月窗口附近波动更大",
        },
        "sample": {
            "start": bars[0]["datetime"],
            "end": bars[-1]["datetime"],
            "bars": len(bars),
            "days": len({row["date"] for row in bars}),
        },
        "results": results,
    }
    RESULT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> None:
    out = run_backtest()
    sample = out["sample"]
    print(f"{sample['start']} → {sample['end']}  {sample['bars']} 根  {sample['days']} 日")
    for row in out["results"]:
        print(
            f"{row['name']}: CAGR={row['cagr']:.1f}% PnL={row['final_pnl']:.0f} "
            f"Sharpe={row['sharpe']:.2f} 峰值DD={row['max_dd_peak_pct']:.1f}% "
            f"开仓={row['opens']} 月正={row['pos_month_pct']:.0f}%"
        )
    print(f"结果写入 {RESULT_PATH}")


if __name__ == "__main__":
    main()
