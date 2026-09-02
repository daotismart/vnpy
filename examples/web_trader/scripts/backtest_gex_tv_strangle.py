"""Backtest GEX next-month short iron condor on SA / IF bars.

Theta harvest: ~45 DTE 14-25 delta iron condor, take-profit at 75% of credit,
roll at 21 DTE. HV / IV Rank always use daily closes; entries and TP/delta/roll
are checked on each bar (daily / 5-minute / 30-minute).
"""

from __future__ import annotations

import json
import math
import sys
from collections import deque
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gex_tv_strangle import (  # noqa: E402
    condor_lots,
    io_strike_step,
    lsp_value,
    pick_iron_condor,
    pick_strangle,
    sa_strike_step,
    select_expiries,
    synthetic_gex_walls,
)
from vnpy_optionmaster.pricing.black_76 import (  # noqa: E402
    calculate_delta,
    calculate_price,
)

DAILY_CACHE = ROOT.joinpath("sa_daily_cache.json")
CACHE_5MIN = ROOT.joinpath("sa_5min_cache.json")
RESULT_PATH = ROOT.joinpath("backtest_gex_tv_strangle_result.json")
PRICETICK = 0.5
OPT_SIZE = 20.0
FUT_SIZE = 20.0
OPT_COMM = 1.5
FUT_COMM = 3.0
RATE = 0.02
CAPITAL = 1_000_000.0
MAX_LOTS = 600
CALENDAR = "czce"
SAMPLE_START = "2023-10-20"
TARGET_CAGR = 0.50
STRIKE_FN = sa_strike_step
UNIVERSE_LABEL = "郑商所SA 5分钟 → 次月纯碱期权铁鹰"
SOURCE_NOTE = "新浪分月合约5分钟拼接；HV/IV Rank 用日线"
KIND = "SA"
INTERVAL = "5m"


def load_daily(min_date: str | None = None, max_date: str | None = None) -> list[dict[str, Any]]:
    if not DAILY_CACHE.exists() or DAILY_CACHE.stat().st_size < 100:
        raise RuntimeError(f"缺少日线缓存: {DAILY_CACHE.name}")
    raw = json.loads(DAILY_CACHE.read_text(encoding="utf-8"))
    out: list[dict[str, Any]] = []
    for row in raw:
        stamp = str(row[0])[:10]
        close = float(row[4])
        if close <= 0:
            continue
        if min_date and stamp < min_date:
            continue
        if max_date and stamp >= max_date:
            continue
        out.append(
            {
                "date": stamp,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": close,
            }
        )
    return out


def load_5min() -> list[dict[str, Any]]:
    if not CACHE_5MIN.exists() or CACHE_5MIN.stat().st_size < 100:
        raise RuntimeError(f"缺少分钟缓存: {CACHE_5MIN.name}")
    raw = json.loads(CACHE_5MIN.read_text(encoding="utf-8"))
    out: list[dict[str, Any]] = []
    for row in raw:
        close = float(row[4])
        if close <= 0:
            continue
        stamp = str(row[0])
        if stamp[:10] < SAMPLE_START:
            continue
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
    if len(out) < 200:
        raise RuntimeError(f"分钟样本不足: {len(out)}")
    return out


def load_bars() -> list[dict[str, Any]]:
    if INTERVAL != "1d":
        return load_5min()
    rows = load_daily(min_date=SAMPLE_START)
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["datetime"] = item["date"]
        out.append(item)
    if len(out) < 50:
        raise RuntimeError(f"日线样本不足: {len(out)}")
    return out


@dataclass
class Params:
    name: str
    roll_dte: int = 21
    target_dte: int = 45
    min_entry_dte: int = 28
    max_entry_dte: int = 65
    iv_rank_min: float = 40.0
    min_delta: float = 0.14
    max_delta: float = 0.25
    wing_steps: int = 3
    min_credit_frac: float = 0.25
    take_profit: float = 0.25
    delta_stop: float = 0.99
    kelly_scale: float = 0.25
    kelly_cap: float = 0.10
    risk_cap: float = 0.06
    lsp_lo: float = 0.0
    lsp_hi: float = 1.0
    hv_expand: float = 9.0
    hv_lookback: int = 20
    lsp_lookback: int = 20
    hedge: bool = False
    iv_premium: float = 0.12
    max_lots: int = 80
    max_books: int = 1
    # condor | strangle
    structure: str = "condor"
    # debit >= stop_credit_mult * entry_credit → 权利金止损；<=0 关闭
    stop_credit_mult: float = 0.0
    # DTE 滑动止盈：持仓早期用 tp_far（更快落袋），近移仓用 tp_near
    dynamic_tp: bool = False
    tp_far: float = 0.50
    tp_near: float = 0.25
    # 现价距短行权价 ≤ wall_stop_steps×步长 → 墙距止损；<=0 关闭
    wall_stop_steps: float = 0.0
    margin_rate: float = 0.12
    min_margin_rate: float = 0.06


PRESETS: list[Params] = [
    Params("原优化6%"),
    Params("进取20%", risk_cap=0.20, max_lots=250),
    Params("容量80手", risk_cap=0.46, max_lots=80),
    Params("IF杠杆46%", risk_cap=0.46, max_lots=600),
]


def _norm_interval(interval: str) -> str:
    raw = str(interval).lower().strip()
    if raw in ("1d", "d", "day", "daily", "1day"):
        return "1d"
    if raw in ("30", "30m", "30min", "30minute"):
        return "30m"
    return "5m"


def configure(kind: str = "SA", interval: str = "5m") -> None:
    global DAILY_CACHE, CACHE_5MIN, RESULT_PATH, PRICETICK, OPT_SIZE, FUT_SIZE
    global OPT_COMM, FUT_COMM, CALENDAR, SAMPLE_START, PRESETS, STRIKE_FN
    global UNIVERSE_LABEL, SOURCE_NOTE, KIND, INTERVAL
    KIND = kind.upper()
    INTERVAL = _norm_interval(interval)
    if KIND == "IF":
        DAILY_CACHE = ROOT.joinpath("if_daily_cache.json")
        CACHE_5MIN = ROOT.joinpath(
            "if_daily_cache.json" if INTERVAL == "1d" else ("if_30min_cache.json" if INTERVAL == "30m" else "if_5min_cache.json")
        )
        result_name = {
            "1d": "backtest_gex_tv_strangle_if_daily_result.json",
            "30m": "backtest_gex_tv_strangle_if_30m_result.json",
            "5m": "backtest_gex_tv_strangle_if_result.json",
        }[INTERVAL]
        RESULT_PATH = ROOT.joinpath(result_name)
        PRICETICK = 0.2
        OPT_SIZE = 100.0
        FUT_SIZE = 300.0
        OPT_COMM = 1.5
        FUT_COMM = 25.0
        CALENDAR = "cffex"
        SAMPLE_START = "2019-12-23"
        STRIKE_FN = io_strike_step
        freq = {"1d": "日线", "30m": "30分钟", "5m": "5分钟"}[INTERVAL]
        UNIVERSE_LABEL = f"中金所IF {freq} → 次月IO铁鹰"
        SOURCE_NOTE = {
            "1d": "IF主力日线；HV/IV Rank 用同一份日线；到期第三周五",
            "30m": "同一份IF 5分钟缓存重采样为30分钟；HV/IV Rank 用IF日线；到期第三周五",
            "5m": "新浪分月IF 5分钟拼接；HV/IV Rank 用IF日线；到期第三周五",
        }[INTERVAL]
        PRESETS = [
            Params("稳健6%"),
            Params("进取20%", risk_cap=0.20, max_lots=80),
            Params("年化50%杠杆", risk_cap=0.46, max_lots=280),
        ]
    else:
        DAILY_CACHE = ROOT.joinpath("sa_daily_cache.json")
        CACHE_5MIN = ROOT.joinpath(
            "sa_daily_cache.json" if INTERVAL == "1d" else ("sa_30min_cache.json" if INTERVAL == "30m" else "sa_5min_cache.json")
        )
        result_name = {
            "1d": "backtest_gex_tv_strangle_daily_result.json",
            "30m": "backtest_gex_tv_strangle_30m_result.json",
            "5m": "backtest_gex_tv_strangle_result.json",
        }[INTERVAL]
        RESULT_PATH = ROOT.joinpath(result_name)
        PRICETICK = 0.5
        OPT_SIZE = 20.0
        FUT_SIZE = 20.0
        OPT_COMM = 1.5
        FUT_COMM = 3.0
        CALENDAR = "czce"
        SAMPLE_START = "2023-10-20"
        STRIKE_FN = sa_strike_step
        freq = {"1d": "日线", "30m": "30分钟", "5m": "5分钟"}[INTERVAL]
        UNIVERSE_LABEL = f"郑商所SA {freq} → 次月纯碱期权铁鹰"
        SOURCE_NOTE = {
            "1d": "SA主力日线；HV/IV Rank 用同一份日线",
            "30m": "同一份SA 5分钟缓存重采样为30分钟；HV/IV Rank 用日线",
            "5m": "新浪分月合约5分钟拼接；HV/IV Rank 用日线",
        }[INTERVAL]
        PRESETS = [
            Params("原优化6%"),
            Params("进取20%", risk_cap=0.20, max_lots=250),
            Params("容量80手", risk_cap=0.46, max_lots=80),
        ]


def preset_payloads() -> list[dict[str, Any]]:
    return [asdict(item) for item in PRESETS]


def cache_meta() -> dict[str, Any]:
    path = DAILY_CACHE if INTERVAL == "1d" else CACHE_5MIN
    info: dict[str, Any] = {
        "exists": False,
        "path": str(path),
        "bars": 0,
        "days": 0,
        "start": "",
        "end": "",
        "kind": KIND,
        "interval": INTERVAL,
    }
    if not path.exists() or path.stat().st_size < 100:
        return info
    try:
        bars = load_bars()
    except Exception as exc:
        info["error"] = str(exc)
        return info
    info["exists"] = True
    info["bars"] = len(bars)
    info["days"] = len({row["date"] for row in bars})
    info["start"] = bars[0]["datetime"]
    info["end"] = bars[-1]["datetime"]
    return info


def load_saved_result() -> dict[str, Any] | None:
    if not RESULT_PATH.exists() or RESULT_PATH.stat().st_size < 20:
        return None
    try:
        data = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def params_from_dict(data: dict[str, Any] | None) -> Params:
    data = data or {}
    return Params(
        name=str(data.get("name") or "自定义参数"),
        roll_dte=int(data.get("roll_dte") or 21),
        target_dte=int(data.get("target_dte") or 45),
        min_entry_dte=int(data.get("min_entry_dte") or 28),
        max_entry_dte=int(data.get("max_entry_dte") or 65),
        iv_rank_min=float(data.get("iv_rank_min") if data.get("iv_rank_min") is not None else 40.0),
        min_delta=float(data.get("min_delta") if data.get("min_delta") is not None else 0.14),
        max_delta=float(data.get("max_delta") if data.get("max_delta") is not None else 0.25),
        wing_steps=int(data.get("wing_steps") or 3),
        min_credit_frac=float(data.get("min_credit_frac") if data.get("min_credit_frac") is not None else 0.25),
        take_profit=float(data.get("take_profit") if data.get("take_profit") is not None else 0.25),
        delta_stop=float(data.get("delta_stop") if data.get("delta_stop") is not None else 0.99),
        kelly_scale=float(data.get("kelly_scale") if data.get("kelly_scale") is not None else 0.25),
        kelly_cap=float(data.get("kelly_cap") if data.get("kelly_cap") is not None else 0.10),
        risk_cap=float(data.get("risk_cap") if data.get("risk_cap") is not None else 0.06),
        lsp_lo=float(data.get("lsp_lo") if data.get("lsp_lo") is not None else 0.0),
        lsp_hi=float(data.get("lsp_hi") if data.get("lsp_hi") is not None else 1.0),
        hv_expand=float(data.get("hv_expand") if data.get("hv_expand") is not None else 9.0),
        hedge=bool(data["hedge"]) if "hedge" in data else False,
        max_lots=int(data.get("max_lots") or 80),
        max_books=int(data.get("max_books") or 1),
        structure=str(data.get("structure") or "condor"),
        stop_credit_mult=float(data.get("stop_credit_mult") if data.get("stop_credit_mult") is not None else 0.0),
        dynamic_tp=bool(data["dynamic_tp"]) if "dynamic_tp" in data else False,
        tp_far=float(data.get("tp_far") if data.get("tp_far") is not None else 0.50),
        tp_near=float(data.get("tp_near") if data.get("tp_near") is not None else 0.25),
        wall_stop_steps=float(data.get("wall_stop_steps") if data.get("wall_stop_steps") is not None else 0.0),
        margin_rate=float(data.get("margin_rate") if data.get("margin_rate") is not None else 0.12),
        min_margin_rate=float(data.get("min_margin_rate") if data.get("min_margin_rate") is not None else 0.06),
    )


def is_strangle(params: Params | _Pos | str) -> bool:
    if isinstance(params, str):
        return params.lower().startswith("strangle")
    structure = getattr(params, "structure", "condor")
    return str(structure).lower().startswith("strangle")


def leg_count(params: Params | _Pos | str) -> int:
    return 2 if is_strangle(params) else 4


def tp_threshold(params: Params, dte: int) -> float:
    """买回成本/入场权利金 ≤ 阈值则止盈。"""
    if not params.dynamic_tp:
        return float(params.take_profit)
    hi_dte = float(params.target_dte)
    lo_dte = float(params.roll_dte)
    if dte >= hi_dte:
        return float(params.tp_far)
    if dte <= lo_dte:
        return float(params.tp_near)
    span = max(hi_dte - lo_dte, 1.0)
    w = (dte - lo_dte) / span
    return float(params.tp_near) + w * (float(params.tp_far) - float(params.tp_near))


def parse_day(text: str) -> date:
    return datetime.strptime(text.replace("-", ""), "%Y%m%d").date() if "-" not in text else datetime.strptime(text[:10], "%Y-%m-%d").date()


def hv_from_closes(closes: list[float], lookback: int) -> float:
    if len(closes) <= lookback:
        return 0.25
    window = np.array(closes[-(lookback + 1) :], dtype=float)
    rets = np.diff(np.log(window))
    if len(rets) < 5:
        return 0.25
    return float(max(0.10, min(1.20, np.std(rets, ddof=1) * math.sqrt(242))))


def iv_rank(value: float, hist: list[float]) -> float:
    if not hist:
        return 50.0
    return 100.0 * sum(1 for item in hist if item <= value) / len(hist)


def mark_greeks(spot: float, k: float, t: float, sigma: float, cp: int) -> tuple[float, float]:
    t = max(t, 1.0 / 365.0)
    sigma = max(sigma, 0.05)
    price = max(calculate_price(spot, k, RATE, t, sigma, cp), PRICETICK)
    delta = calculate_delta(spot, k, RATE, t, sigma, cp)
    return float(price), float(delta)


def calendar_years(start: str, end: str) -> float:
    return max((parse_day(end) - parse_day(start)).days / 365.25, 1e-9)


@dataclass
class _Pos:
    lots: int
    k_call: float
    k_put: float
    k_call_l: float
    k_put_l: float
    expiry: date
    entry_credit: float
    fut: float = 0.0
    structure: str = "condor"


def seed_daily(params: Params) -> tuple[deque[float], deque[float], deque[float], list[float]]:
    warmup = load_daily(max_date=SAMPLE_START)
    highs: deque[float] = deque(maxlen=80)
    lows: deque[float] = deque(maxlen=80)
    closes: deque[float] = deque(maxlen=80)
    hv_hist: list[float] = []
    for row in warmup:
        highs.append(row["high"])
        lows.append(row["low"])
        closes.append(row["close"])
        hv_hist.append(hv_from_closes(list(closes), params.hv_lookback) * (1.0 + params.iv_premium))
    return highs, lows, closes, hv_hist[-60:]


def run_one(bars: list[dict[str, Any]], params: Params) -> dict[str, Any]:
    cash = 0.0
    books: list[_Pos] = []
    prev_spot = float(bars[0]["close"])
    day_highs, day_lows, day_closes, hv_hist = seed_daily(params)

    trades: list[dict[str, Any]] = []
    open_set: set[str] = set()
    skip_set: set[str] = set()
    rolls = 0
    stops = 0
    stops_delta = 0
    stops_credit = 0
    stops_wall = 0
    take_profits = 0
    peak = CAPITAL
    max_dd = 0.0
    max_dd_peak_pct = 0.0
    last_nav = CAPITAL
    lot_cap = int(params.max_lots or MAX_LOTS)
    session_high = 0.0
    session_low = 0.0
    session_close = 0.0
    prev_day = ""
    day_nav: dict[str, float] = {}
    day_lsp: dict[str, float] = {}
    day_iv: dict[str, float] = {}
    day_rank: dict[str, float] = {}
    day_lots: dict[str, int] = {}
    day_delta: dict[str, float] = {}
    n_legs = leg_count(params)

    def mark_pos(pos: _Pos, spot: float, today: date, iv: float) -> tuple[float, float, float, float, float]:
        t = max((pos.expiry - today).days, 1) / 365.0
        pc, dc = mark_greeks(spot, pos.k_call, t, iv, 1)
        pp, dp = mark_greeks(spot, pos.k_put, t, iv, -1)
        if is_strangle(pos):
            debit = pc + pp
            opt = -pos.lots * debit * OPT_SIZE
            delta = pos.lots * (-dc - dp) * OPT_SIZE * spot
            return opt, debit, abs(dc), abs(dp), delta
        pcl, dcl = mark_greeks(spot, pos.k_call_l, t, iv, 1)
        ppl, dpl = mark_greeks(spot, pos.k_put_l, t, iv, -1)
        debit = (pc + pp) - (pcl + ppl)
        opt = -pos.lots * debit * OPT_SIZE
        delta = pos.lots * (-dc - dp + dcl + dpl) * OPT_SIZE * spot
        return opt, debit, abs(dc), abs(dp), delta

    def value_all(spot: float, today: date, iv: float) -> tuple[float, float]:
        opt_mtm = 0.0
        delta = 0.0
        for pos in books:
            opt, _, _, _, dlt = mark_pos(pos, spot, today, iv)
            opt_mtm += opt
            delta += dlt + pos.fut * spot * FUT_SIZE
        return opt_mtm, delta

    def flatten(pos: _Pos, reason: str, row: dict[str, Any], spot: float, today: date, iv: float) -> None:
        nonlocal cash, prev_spot
        dte = (pos.expiry - today).days
        if pos.fut:
            cash += pos.fut * (spot - prev_spot) * FUT_SIZE
            cash -= abs(pos.fut) * FUT_COMM
        _, debit, _, _, _ = mark_pos(pos, spot, today, iv)
        cash -= pos.lots * debit * OPT_SIZE
        cash -= leg_count(pos) * pos.lots * OPT_COMM
        trades.append(
            {
                "date": row.get("datetime") or row["date"],
                "action": reason,
                "structure": pos.structure,
                "k_put": pos.k_put,
                "k_call": pos.k_call,
                "k_put_long": pos.k_put_l,
                "k_call_long": pos.k_call_l,
                "lots": pos.lots,
                "debit": round(debit, 2),
                "entry_credit": round(pos.entry_credit, 2),
                "dte": dte,
                "expiry": pos.expiry.isoformat(),
            }
        )

    def try_open(row: dict[str, Any], today: date, spot: float, iv: float, rank: float, lsp: float, nav: float) -> bool:
        nonlocal cash
        held = {pos.expiry for pos in books}
        slots = max(int(params.max_books) - len(books), 0)
        if slots <= 0:
            return False
        exps = select_expiries(
            today, CALENDAR, params.target_dte, params.min_entry_dte, params.max_entry_dte, held, slots
        )
        opened = False
        working_nav = nav
        for exp in exps:
            t = max((exp - today).days, 1) / 365.0
            step = STRIKE_FN(spot)
            call_wall, put_wall = synthetic_gex_walls(spot, iv, t, step, OPT_SIZE)
            if is_strangle(params):
                pick = pick_strangle(
                    spot,
                    iv,
                    t,
                    step,
                    OPT_SIZE,
                    call_wall,
                    put_wall,
                    params.min_delta,
                    params.max_delta,
                    params.margin_rate,
                    params.min_margin_rate,
                    PRICETICK,
                )
            else:
                pick = pick_iron_condor(
                    spot,
                    iv,
                    t,
                    step,
                    OPT_SIZE,
                    call_wall,
                    put_wall,
                    params.min_delta,
                    params.max_delta,
                    params.wing_steps,
                    params.min_credit_frac,
                    PRICETICK,
                )
            if pick is None:
                continue
            n_lots, kb = condor_lots(
                pick, max(working_nav, 1.0), params.kelly_scale, params.kelly_cap, params.risk_cap, lot_cap
            )
            if n_lots < 1:
                continue
            cash += n_lots * pick.credit * OPT_SIZE
            cash -= n_legs * n_lots * OPT_COMM
            working_nav -= n_legs * n_lots * OPT_COMM
            books.append(
                _Pos(
                    lots=n_lots,
                    k_call=pick.k_call,
                    k_put=pick.k_put,
                    k_call_l=pick.k_call_long,
                    k_put_l=pick.k_put_long,
                    expiry=exp,
                    entry_credit=pick.credit,
                    structure="strangle" if is_strangle(params) else "condor",
                )
            )
            trades.append(
                {
                    "date": row.get("datetime") or row["date"],
                    "action": "开仓",
                    "structure": "strangle" if is_strangle(params) else "condor",
                    "k_put": pick.k_put,
                    "k_call": pick.k_call,
                    "k_put_long": pick.k_put_long,
                    "k_call_long": pick.k_call_long,
                    "lots": n_lots,
                    "credit": round(pick.credit, 2),
                    "dte": (exp - today).days,
                    "expiry": exp.isoformat(),
                    "kelly": round(kb.f, 4),
                    "kelly_raw": round(kb.f_raw, 4),
                    "budget": round(kb.budget, 0),
                    "max_loss": round(pick.max_loss, 0),
                    "win_prob": round(pick.win_prob, 4),
                    "p_call_win": round(pick.p_call_win, 4),
                    "p_put_win": round(pick.p_put_win, 4),
                    "payoff_ratio": round(pick.payoff_ratio, 4),
                    "efficiency": round(pick.efficiency, 6),
                    "iv_rank": round(rank, 1),
                    "lsp": round(lsp, 3),
                }
            )
            opened = True
        return opened

    for row in bars:
        stamp = row["datetime"]
        day = row["date"]
        today = parse_day(day)
        hi, lo, cl = float(row["high"]), float(row["low"]), float(row["close"])
        if day != prev_day:
            if prev_day and session_high > 0:
                day_highs.append(session_high)
                day_lows.append(session_low)
                day_closes.append(session_close)
                hv_hist.append(hv_from_closes(list(day_closes), params.hv_lookback) * (1.0 + params.iv_premium))
                hv_hist = hv_hist[-60:]
            session_high, session_low, session_close = hi, lo, cl
            prev_day = day
        else:
            session_high = max(session_high, hi)
            session_low = min(session_low, lo) if session_low else lo
            session_close = cl

        hv = hv_from_closes(list(day_closes), params.hv_lookback)
        hv60 = hv_from_closes(list(day_closes), 60)
        iv = hv * (1.0 + params.iv_premium)
        rank = iv_rank(iv, hv_hist)
        look_h = list(day_highs)[-params.lsp_lookback + 1 :] + [session_high]
        look_l = list(day_lows)[-params.lsp_lookback + 1 :] + [session_low]
        lsp = lsp_value(look_h, look_l, cl)

        for pos in books:
            cash += pos.fut * (cl - prev_spot) * FUT_SIZE

        keep: list[_Pos] = []
        for pos in books:
            dte = (pos.expiry - today).days
            step = STRIKE_FN(cl)
            _, debit_hi, d_call_hi, _, _ = mark_pos(pos, hi, today, iv)
            _, debit_lo, _, d_put_lo, _ = mark_pos(pos, lo, today, iv)
            _, debit, _, _, _ = mark_pos(pos, cl, today, iv)
            call_hit = d_call_hi >= params.delta_stop
            put_hit = d_put_lo >= params.delta_stop
            credit_stop = (
                params.stop_credit_mult > 0
                and pos.entry_credit > 0
                and max(debit, debit_hi, debit_lo) >= params.stop_credit_mult * pos.entry_credit
            )
            wall_hit = False
            if params.wall_stop_steps > 0:
                buf = params.wall_stop_steps * step
                wall_hit = hi >= pos.k_call - buf or lo <= pos.k_put + buf
            take_profit = pos.entry_credit > 0 and debit <= tp_threshold(params, dte) * pos.entry_credit
            if call_hit or put_hit or credit_stop or wall_hit:
                if call_hit and put_hit:
                    stop_px = hi if debit_hi >= debit_lo else lo
                elif call_hit or (wall_hit and hi >= pos.k_call - max(params.wall_stop_steps, 0) * step):
                    stop_px = hi
                elif put_hit or wall_hit:
                    stop_px = lo
                else:
                    stop_px = hi if debit_hi >= debit_lo else lo
                if call_hit or put_hit:
                    reason = "止损-Delta"
                    stops_delta += 1
                elif wall_hit:
                    reason = "止损-墙距"
                    stops_wall += 1
                else:
                    reason = "止损-权利金"
                    stops_credit += 1
                flatten(pos, reason, row, stop_px, today, iv)
                stops += 1
            elif take_profit:
                flatten(pos, "止盈", row, cl, today, iv)
                take_profits += 1
            elif dte <= params.roll_dte:
                flatten(pos, "移仓", row, cl, today, iv)
                rolls += 1
            else:
                keep.append(pos)
        books = keep

        opt_mtm, _ = value_all(cl, today, iv)
        nav = CAPITAL + cash + opt_mtm
        iv_high = rank >= params.iv_rank_min
        range_ok = params.lsp_lo <= lsp <= params.lsp_hi
        expand_ok = hv <= params.hv_expand * max(hv60, 1e-6)
        if len(books) < int(params.max_books):
            if iv_high and range_ok and expand_ok:
                try_open(row, today, cl, iv, rank, lsp, nav)
            elif not books:
                skip_set.add(day)
        if books:
            open_set.add(day)

        opt_mtm, opt_delta = value_all(cl, today, iv)
        nav = CAPITAL + cash + opt_mtm
        total_fut = sum(pos.fut for pos in books)
        if params.hedge and books:
            need_fut = (0.0 - opt_delta) / max(cl * FUT_SIZE, 1.0)
            diff = need_fut - total_fut
            if abs(diff) >= 0.5:
                step_n = int(round(diff))
                if step_n:
                    cash -= abs(step_n) * FUT_COMM
                    books[0].fut += step_n
            opt_mtm, opt_delta = value_all(cl, today, iv)
            nav = CAPITAL + cash + opt_mtm
        elif not books and total_fut:
            cash -= abs(total_fut) * FUT_COMM
            nav = CAPITAL + cash

        last_nav = nav
        peak = max(peak, nav)
        max_dd = min(max_dd, nav - peak)
        if peak > 0:
            max_dd_peak_pct = min(max_dd_peak_pct, (nav - peak) / peak)
        day_nav[day] = nav
        day_lsp[day] = lsp
        day_iv[day] = iv
        day_rank[day] = rank
        day_lots[day] = sum(pos.lots for pos in books)
        day_delta[day] = opt_delta
        prev_spot = cl
        if nav <= 0:
            break

    if books:
        last_nav = last_nav - sum(leg_count(pos) * pos.lots * OPT_COMM + abs(pos.fut) * FUT_COMM for pos in books)
        keys = sorted(day_nav)
        if keys:
            day_nav[keys[-1]] = last_nav
        books = []

    keys = sorted(day_nav)
    n = len(keys)
    eq = np.array([day_nav[k] for k in keys], dtype=float)
    pnl = np.r_[eq[0] - CAPITAL, np.diff(eq)]
    prev_eq = np.concatenate(([CAPITAL], eq[:-1]))
    nav_rets = pnl / np.maximum(prev_eq, 1.0)
    sharpe = float(np.mean(nav_rets) / (np.std(nav_rets) + 1e-9) * math.sqrt(242)) if len(nav_rets) > 2 else 0.0
    win = float(np.mean(pnl > 0)) if len(pnl) else 0.0
    years = calendar_years(keys[0], keys[-1])
    final_nav = float(eq[-1])
    cagr = (max(final_nav, 1e-9) / CAPITAL) ** (1.0 / years) - 1.0
    months: dict[str, float] = {}
    yearly: dict[str, float] = {}
    for day, p in zip(keys, pnl):
        months[day[:7]] = round(months.get(day[:7], 0.0) + float(p), 2)
        yearly[day[:4]] = round(yearly.get(day[:4], 0.0) + float(p), 2)

    step = max(1, n // 48)
    eq_x = [keys[i][2:7] for i in range(0, n, step)]
    eq_y = [round(float(eq[i]) - CAPITAL, 1) for i in range(0, n, step)]
    if eq_x[-1] != keys[-1][2:7]:
        eq_x.append(keys[-1][2:7])
        eq_y.append(round(float(eq[-1]) - CAPITAL, 1))

    opens = [t for t in trades if t["action"] == "开仓"]
    avg_kelly = float(np.mean([t.get("kelly") or 0 for t in opens])) if opens else 0.0
    avg_winp = float(np.mean([t.get("win_prob") or 0 for t in opens])) if opens else 0.0
    avg_p_call = float(np.mean([t.get("p_call_win") or 0 for t in opens])) if opens else 0.0
    avg_p_put = float(np.mean([t.get("p_put_win") or 0 for t in opens])) if opens else 0.0
    avg_eff = float(np.mean([t.get("efficiency") or 0 for t in opens])) if opens else 0.0
    open_lots = [float(t.get("lots") or 0) for t in opens]
    avg_open_lots = float(np.mean(open_lots)) if open_lots else 0.0
    min_open_lots = float(np.min(open_lots)) if open_lots else 0.0
    max_open_lots = float(np.max(open_lots)) if open_lots else 0.0
    lots_series = [day_lots[k] for k in keys]
    pos_months = sum(1 for v in months.values() if v > 0)
    month_count = max(len(months), 1)
    calmar = (cagr * 100.0) / abs(max_dd_peak_pct) if abs(max_dd_peak_pct) > 1e-9 else 0.0

    return {
        "name": params.name,
        "start": bars[0]["datetime"],
        "end": bars[-1]["datetime"],
        "bars": len(bars),
        "days": n,
        "years": round(years, 3),
        "cagr": round(cagr * 100, 2),
        "final_pnl": round(final_nav - CAPITAL, 2),
        "final_nav": round(final_nav, 2),
        "sharpe": round(sharpe, 3),
        "calmar": round(calmar, 3),
        "max_dd": round(float(max_dd), 2),
        "max_dd_pct": round(100.0 * float(max_dd) / CAPITAL, 2),
        "max_dd_peak_pct": round(100.0 * max_dd_peak_pct, 2),
        "win_rate": round(win * 100, 1),
        "open_days": len(open_set),
        "rolls": rolls,
        "stops": stops,
        "stops_delta": stops_delta,
        "stops_credit": stops_credit,
        "stops_wall": stops_wall,
        "take_profits": take_profits,
        "opens": len(opens),
        "skip_iv": len(skip_set),
        "avg_kelly": round(avg_kelly, 4),
        "avg_win_prob": round(avg_winp, 4),
        "avg_p_call_win": round(avg_p_call, 4),
        "avg_p_put_win": round(avg_p_put, 4),
        "avg_theta_efficiency": round(avg_eff, 6),
        "avg_lots": round(float(np.mean(lots_series)), 2) if lots_series else 0.0,
        "avg_open_lots": round(avg_open_lots, 2),
        "min_open_lots": int(min_open_lots),
        "max_open_lots": int(max_open_lots),
        "pos_month_pct": round(100.0 * pos_months / month_count, 1),
        "hedge": params.hedge,
        "iv_rank_min": params.iv_rank_min,
        "kelly_scale": params.kelly_scale,
        "monthly": months,
        "yearly": yearly,
        "equity_x": eq_x,
        "equity_y": eq_y,
        "lsp_y": [round(float(day_lsp[keys[i]]), 3) for i in range(0, n, step)],
        "iv_y": [round(float(day_iv[keys[i]]), 4) for i in range(0, n, step)],
        "rank_y": [round(float(day_rank[keys[i]]), 1) for i in range(0, n, step)],
        "delta_y": [round(float(day_delta[keys[i]]) / 1e4, 2) for i in range(0, n, step)],
        "trades": trades[-40:],
        "daily_mean": round(float(np.mean(pnl)), 2),
        "daily_std": round(float(np.std(pnl)), 2),
        "best_day": round(float(np.max(pnl)), 2),
        "worst_day": round(float(np.min(pnl)), 2),
        **asdict(params),
    }


def _print_row(row: dict[str, Any]) -> None:
    print(
        f"{row['name']}: CAGR={row['cagr']:.1f}% PnL={row['final_pnl']:.0f} "
        f"Sharpe={row['sharpe']:.2f} Calmar={row.get('calmar', 0):.2f} "
        f"峰值DD={row['max_dd_peak_pct']:.1f}% "
        f"开仓={row['opens']} 止盈={row['take_profits']} 止损={row['stops']}"
        f"(Δ{row.get('stops_delta', 0)}/权{row.get('stops_credit', 0)}/墙{row.get('stops_wall', 0)}) "
        f"移仓={row['rolls']} 手数={row['min_open_lots']}-{row['max_open_lots']}"
    )


def structure_compare_presets() -> list[Params]:
    """铁鹰 vs 宽跨 × 静态/动态出场 对照网格（IF 日线）。"""
    dyn = dict(
        dynamic_tp=True,
        tp_far=0.50,
        tp_near=0.25,
        stop_credit_mult=2.0,
        delta_stop=0.35,
        wall_stop_steps=1.0,
    )
    return [
        Params(
            "铁鹰基线-IO实盘推荐",
            structure="condor",
            wing_steps=5,
            min_credit_frac=0.30,
            risk_cap=0.06,
            max_lots=80,
            take_profit=0.25,
            delta_stop=0.99,
        ),
        Params(
            "铁鹰+动态出场",
            structure="condor",
            wing_steps=5,
            min_credit_frac=0.30,
            risk_cap=0.06,
            max_lots=80,
            take_profit=0.25,
            **dyn,
        ),
        Params(
            "宽跨-静态出场-风险6%",
            structure="strangle",
            min_delta=0.14,
            max_delta=0.25,
            risk_cap=0.06,
            max_lots=80,
            take_profit=0.25,
            delta_stop=0.99,
        ),
        Params(
            "宽跨+动态出场-风险6%",
            structure="strangle",
            min_delta=0.14,
            max_delta=0.25,
            risk_cap=0.06,
            max_lots=80,
            take_profit=0.25,
            **dyn,
        ),
        Params(
            "宽跨+动态出场-风险3%",
            structure="strangle",
            min_delta=0.14,
            max_delta=0.25,
            risk_cap=0.03,
            max_lots=40,
            take_profit=0.25,
            **dyn,
        ),
        Params(
            "宽跨宽Δ+动态-风险3%",
            structure="strangle",
            min_delta=0.08,
            max_delta=0.28,
            risk_cap=0.03,
            max_lots=40,
            take_profit=0.25,
            **dyn,
        ),
    ]


def run_structure_compare(kind: str = "IF", interval: str = "1d") -> dict[str, Any]:
    configure(kind, interval)
    bars = load_bars()
    presets = structure_compare_presets()
    results = []
    for item in presets:
        row = run_one(bars, item)
        results.append(row)
        _print_row(row)
        sys.stdout.flush()

    baseline = next((r for r in results if r["name"].startswith("铁鹰基线")), results[0])
    base_calmar = float(baseline.get("calmar") or 0.0)
    ranking = sorted(results, key=lambda r: (r.get("calmar") or 0, r.get("sharpe") or 0), reverse=True)
    verdict_rows = []
    for row in ranking:
        half_ok = float(row.get("calmar") or 0) >= 0.5 * max(base_calmar, 1e-9)
        verdict_rows.append(
            {
                "name": row["name"],
                "structure": row.get("structure"),
                "sharpe": row["sharpe"],
                "calmar": row.get("calmar"),
                "cagr": row["cagr"],
                "max_dd_peak_pct": row["max_dd_peak_pct"],
                "opens": row["opens"],
                "stops": row["stops"],
                "take_profits": row["take_profits"],
                "rolls": row["rolls"],
                "pass_half_calmar_vs_baseline": half_ok,
            }
        )
    best_strangle = next((r for r in ranking if str(r.get("structure")).startswith("strangle")), None)
    recommend = "keep_condor"
    if best_strangle and float(best_strangle.get("calmar") or 0) >= 0.5 * max(base_calmar, 1e-9):
        if float(best_strangle.get("calmar") or 0) >= base_calmar and float(best_strangle.get("sharpe") or 0) >= float(
            baseline.get("sharpe") or 0
        ):
            recommend = "consider_strangle_pilot"
        else:
            recommend = "strangle_research_only"

    out = {
        "generated": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "engine": "gex-structure-compare",
        "kind": KIND,
        "universe": f"{UNIVERSE_LABEL}（铁鹰 vs 宽跨对照）",
        "interval": INTERVAL,
        "capital": CAPITAL,
        "assumptions": {
            "option_size": OPT_SIZE,
            "futures_size": FUT_SIZE,
            "condor": "5翼/权利金≥30%，风险单元=翼宽有界最大亏损",
            "strangle": "GEX墙外短跨，风险单元=卖方保证金(max+0.5min)",
            "static_exit": "收回75%权利金止盈；短腿Δ≥0.99止损；DTE≤21移仓",
            "dynamic_exit": (
                "DTE滑动止盈(远月买回≤50%权利金→近移仓≤25%)；"
                "权利金止损 debit≥2×credit；Δ止损≥0.35；墙距≤1×步长"
            ),
            "opt_commission": OPT_COMM,
            "pricetick": PRICETICK,
            "source": SOURCE_NOTE,
            "pricing": "Black-76；不含跳空滑点与保证金追保",
            "gate": "上线门槛：宽跨 Calmar ≥ 铁鹰基线一半",
        },
        "sample": {
            "start": bars[0]["datetime"],
            "end": bars[-1]["datetime"],
            "bars": len(bars),
            "days": len({row["date"] for row in bars}),
        },
        "baseline": baseline["name"],
        "recommend": recommend,
        "ranking": verdict_rows,
        "results": results,
    }
    out_path = ROOT.joinpath("backtest_strangle_vs_condor_if_daily_result.json")
    if KIND != "IF" or INTERVAL != "1d":
        out_path = ROOT.joinpath(f"backtest_strangle_vs_condor_{KIND.lower()}_{INTERVAL}_result.json")
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"推荐={recommend}  结果写入 {out_path}")
    return out


def run_cagr_sweep() -> list[dict[str, Any]]:
    bars = load_bars()
    grid: list[Params] = [
        Params("对照-原6%单仓", risk_cap=0.06, max_lots=20),
        Params("原结构-风险20%-单仓", risk_cap=0.20, max_lots=80),
        Params("原结构-风险30%-单仓", risk_cap=0.30, max_lots=120),
        Params("新结构-风险18%-双仓", risk_cap=0.18, max_books=2),
    ]
    rows: list[dict[str, Any]] = []
    for item in grid:
        row = run_one(bars, item)
        rows.append(row)
        _print_row(row)
        sys.stdout.flush()
    rows.sort(key=lambda r: r["cagr"], reverse=True)
    print("--- ranked by CAGR ---")
    for row in rows:
        _print_row(row)
    return rows


def run_backtest(
    params: Params | None = None,
    compare: bool = True,
    kind: str | None = None,
    interval: str | None = None,
) -> dict[str, Any]:
    if kind is not None or interval is not None:
        configure(kind or KIND, interval or INTERVAL)
    bars = load_bars()
    if compare:
        variants = list(PRESETS)
    elif params is not None:
        variants = [params]
    else:
        variants = PRESETS[:1]
    results = [run_one(bars, item) for item in variants]
    lead = results[0]
    out = {
        "generated": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "engine": "gex",
        "kind": KIND,
        "universe": UNIVERSE_LABEL,
        "interval": INTERVAL,
        "capital": CAPITAL,
        "assumptions": {
            "option_size": OPT_SIZE,
            "futures_size": FUT_SIZE,
            "structure": f"iron condor, wings={lead.get('wing_steps', 3)}×行权价距, max_books={lead.get('max_books', 1)}",
            "opt_commission": OPT_COMM,
            "fut_commission": FUT_COMM,
            "pricetick": PRICETICK,
            "kelly": f"单笔最大亏损≤{100.0 * float(lead.get('risk_cap', 0.06)):.0f}%净值；RN凯利只作上限",
            "entry": (
                f"IV Rank≥{lead.get('iv_rank_min', 40):.0f}，目标{lead.get('target_dte', 45)} DTE"
                f"（{lead.get('min_entry_dte', 28)}–{lead.get('max_entry_dte', 65)}），"
                f"{100 * float(lead.get('min_delta', 0.14)):.0f}–{100 * float(lead.get('max_delta', 0.25)):.0f}Δ短腿，"
                f"净权利金/翼宽≥{100 * float(lead.get('min_credit_frac', 0.25)):.0f}%"
            ),
            "exit": (
                f"收回{100 * (1.0 - float(lead.get('take_profit', 0.25))):.0f}%权利金止盈；"
                f"{'日线' if INTERVAL == '1d' else INTERVAL} 高/低触及短腿Δ≥{lead.get('delta_stop', 0.99)}止损；"
                f"DTE≤{lead.get('roll_dte', 21)}移仓"
            ),
            "hedge": "铁鹰本身近Delta中性，默认不对冲",
            "gex": "墙外选最高净θ/最大亏损",
            "source": SOURCE_NOTE,
            "pricing": "Black-76（IO 欧式；SA 美式按欧式近似）",
            "expiry": "IO 到期第三周五；SA 为交割月前一个月15日之前倒数第3个交易日" if KIND == "SA" else "中金所第三周五",
            "target_cagr": TARGET_CAGR,
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
    out = run_backtest(compare=True)
    sample = out["sample"]
    print(f"{sample['start']} → {sample['end']}  {sample['bars']} 根  {sample['days']} 日")
    for row in out["results"]:
        _print_row(row)
    print(f"结果写入 {RESULT_PATH}")


if __name__ == "__main__":
    kind = "SA"
    interval = "5m"
    mode = "backtest"
    args = [a for a in sys.argv[1:] if a not in ("sweep", "structures", "compare")]
    if "sweep" in sys.argv[1:]:
        mode = "sweep"
    elif "structures" in sys.argv[1:] or "compare" in sys.argv[1:]:
        mode = "structures"
    for arg in args:
        token = arg.lower()
        if token in ("if", "sa"):
            kind = token
        elif token in ("30", "30m", "30min"):
            interval = "30m"
        elif token in ("5", "5m", "5min"):
            interval = "5m"
        elif token in ("1d", "d", "day", "daily"):
            interval = "1d"
        elif token in ("if30", "sa30"):
            kind = token[:2]
            interval = "30m"
        elif token in ("ifd", "sad", "ifdaily", "sadaily"):
            kind = token[:2]
            interval = "1d"
    configure(kind, interval)
    if mode == "sweep":
        run_cagr_sweep()
    elif mode == "structures":
        run_structure_compare(kind, interval)
    else:
        main()
