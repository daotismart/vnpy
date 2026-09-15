"""L1 期权做市回测：用生产录制的 IF/IO Tick 重放 Avellaneda-Stoikov 报价。

相对 backtest_as_option_mm.py（沪深300 30分钟合成链）：
1. 使用真实 L1 BBO（bid1/ask1 + 量）
2. 成交模型为「挂单滞后一拍 + 触价成交」（确定性）
3. 默认做近月 ATM Call/Put，可选 IF Delta 对冲

数据优先本地缓存，其次生产 Web API（/data/tick）。
"""

from __future__ import annotations

import argparse
import os
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from vnpy_optionmaster.pricing.black_76 import (
    calculate_delta,
    calculate_gamma,
    calculate_price,
    calculate_vega,
)

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "l1_mm_tick_cache"
RESULT_PATH = ROOT / "backtest_l1_option_mm_result.json"
REPORT_PATH = ROOT / "backtest_l1_option_mm_report.md"

PRICETICK = 0.2
STRIKE_STEP = 50.0
OPT_SIZE = 100.0
FUT_SIZE = 300.0
OPT_COMM = 1.5
FUT_COMM = 25.0
RATE = 0.02
CAPITAL = 200_000.0

DEFAULT_API = os.environ.get("VN_WEB_API", "http://129.211.55.75:8000")
DEFAULT_USER = os.environ.get("VN_WEB_USER", "admin")
DEFAULT_PASS = os.environ.get("VN_WEB_PASS", "")

EXPIRY = {
    "IO2609": date(2026, 9, 18),
    "IO2610": date(2026, 10, 16),
}


@dataclass
class Params:
    name: str = "L1实盘默认"
    gamma: float = 0.08
    kappa: float = 1.4
    sigma: float = 0.22
    tau_days: float = 0.15
    theo_weight: float = 0.65
    min_spread_ticks: int = 2
    max_spread_ticks: int = 40
    vol_spread: float = 0.015
    gamma_spread_ticks: float = 1.0
    otm_spread_ticks: float = 1.5
    inventory_spread_ticks: float = 1.0
    max_pos: int = 10
    quote_volume: int = 1
    flatten: float = 0.75
    hedge: bool = True
    hedge_lots: float = 1.0
    spread_mult: float = 0.02
    atm_strikes: int = 0
    fill_mode: str = "touch"  # touch | cross
    fill_at: str = "ours"  # ours | market


PRESETS: list[Params] = [
    Params("L1实盘默认", hedge=True),
    Params("L1更紧价差", gamma=0.04, kappa=2.2, min_spread_ticks=1, hedge=True),
    Params("L1更厌恶库存", gamma=0.16, kappa=1.4, hedge=True),
    Params("L1不对冲", hedge=False),
    Params(
        "L1固定2跳",
        gamma=0.001,
        kappa=8.0,
        min_spread_ticks=2,
        vol_spread=0.0,
        spread_mult=0.0001,
        hedge=True,
    ),
]


@dataclass
class Quote:
    bid: float = 0.0
    ask: float = 0.0
    bid_vol: int = 0
    ask_vol: int = 0
    mid: float = 0.0
    theo: float = 0.0
    allow_bid: bool = False
    allow_ask: bool = False


@dataclass
class Book:
    bid: float = 0.0
    ask: float = 0.0
    bid_vol: float = 0.0
    ask_vol: float = 0.0
    last: float = 0.0
    volume: float = 0.0
    dt: datetime | None = None


@dataclass
class LegState:
    symbol: str
    cp: int
    strike: float
    pos: int = 0
    quote: Quote = field(default_factory=Quote)
    delta: float = 0.0
    gamma: float = 0.0
    vega: float = 0.0
    theo: float = 0.0
    mid: float = 0.0


def floor_to(value: float, tick: float) -> float:
    return math.floor(value / tick + 1e-12) * tick


def ceil_to(value: float, tick: float) -> float:
    return math.ceil(value / tick - 1e-12) * tick


def round_to(value: float, tick: float) -> float:
    return round(value / tick) * tick


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


def as_quotes(
    mid: float,
    inventory: float,
    gamma: float,
    kappa: float,
    sigma: float,
    tau_days: float,
    spread_mult: float,
) -> tuple[float, float]:
    gamma = max(gamma, 1e-6)
    kappa = max(kappa, 1e-6)
    sigma = max(sigma, 1e-4)
    tau = max(tau_days, 1 / 365) / 365.0
    q = max(-1.0, min(1.0, inventory))
    reservation = mid - q * gamma * (sigma ** 2) * tau * mid
    half = (
        0.5 * gamma * (sigma ** 2) * tau * mid
        + (1.0 / gamma) * math.log(1.0 + gamma / kappa) * mid * spread_mult
    )
    return reservation, max(half, 0.0)


def calc_greeks(spot: float, strike: float, tte: float, sigma: float, cp: int) -> tuple[float, float, float, float]:
    tte = max(tte, 1 / 365)
    sigma = max(sigma, 0.05)
    price = float(calculate_price(spot, strike, RATE, tte, sigma, cp))
    delta = float(calculate_delta(spot, strike, RATE, tte, sigma, cp))
    gamma = float(calculate_gamma(spot, strike, RATE, tte, sigma))
    vega = float(calculate_vega(spot, strike, RATE, tte, sigma))
    return price, delta, gamma, vega


def year_fraction(day: date, expiry: date) -> float:
    return max((expiry - day).days, 1) / 365.0


def build_quote(
    params: Params,
    theo: float,
    market_mid: float,
    bid_mkt: float,
    ask_mkt: float,
    pos: int,
    unit_delta: float,
    vega: float,
    rel_gamma: float,
) -> Quote:
    if theo > 0 and market_mid > 0:
        mid = params.theo_weight * theo + (1.0 - params.theo_weight) * market_mid
    else:
        mid = theo or market_mid
    if mid <= 0:
        return Quote()

    inv = pos / max(params.max_pos, 1)
    reservation, as_half = as_quotes(
        mid, inv, params.gamma, params.kappa, params.sigma, params.tau_days, params.spread_mult
    )
    vega_half = params.vol_spread * abs(vega) / OPT_SIZE / 2.0
    gamma_half = params.gamma_spread_ticks * PRICETICK * rel_gamma / 2.0
    otm_half = params.otm_spread_ticks * PRICETICK * abs(abs(unit_delta) - 0.5) / 0.5
    inv_half = params.inventory_spread_ticks * PRICETICK * abs(inv)
    min_half = params.min_spread_ticks * PRICETICK / 2.0
    half = max(as_half, vega_half, gamma_half, otm_half, inv_half, min_half)
    half = min(half, params.max_spread_ticks * PRICETICK / 2.0)

    bid = floor_to(reservation - half, PRICETICK)
    ask = ceil_to(reservation + half, PRICETICK)
    if ask <= bid:
        ask = bid + PRICETICK
    if ask_mkt > 0:
        bid = min(bid, ask_mkt - PRICETICK)
    if bid_mkt > 0:
        ask = max(ask, bid_mkt + PRICETICK)
    if bid <= 0 or ask <= bid:
        return Quote(mid=mid, theo=theo)

    allow_bid = pos < params.max_pos
    allow_ask = pos > -params.max_pos
    if abs(inv) >= params.flatten and pos > 0:
        allow_bid = False
    if abs(inv) >= params.flatten and pos < 0:
        allow_ask = False

    return Quote(
        bid=bid,
        ask=ask,
        bid_vol=params.quote_volume if allow_bid else 0,
        ask_vol=params.quote_volume if allow_ask else 0,
        mid=mid,
        theo=theo,
        allow_bid=allow_bid and bid > 0,
        allow_ask=allow_ask and ask > bid,
    )


def try_fill(quote: Quote, book: Book, params: Params) -> tuple[int, float]:
    """返回 (side, price)：+1 买 / -1 卖 / 0 无成交。"""
    if book.bid <= 0 or book.ask <= 0 or book.ask < book.bid:
        return 0, 0.0

    eps = 1e-12 if params.fill_mode == "touch" else 0.0
    buy_hit = quote.allow_bid and quote.bid + eps >= book.ask
    sell_hit = quote.allow_ask and quote.ask - eps <= book.bid

    if buy_hit and sell_hit:
        mid = (book.bid + book.ask) / 2
        if abs(quote.bid - mid) <= abs(quote.ask - mid):
            sell_hit = False
        else:
            buy_hit = False

    if buy_hit and book.ask_vol > 0:
        px = quote.bid if params.fill_at == "ours" else book.ask
        return 1, px
    if sell_hit and book.bid_vol > 0:
        px = quote.ask if params.fill_at == "ours" else book.bid
        return -1, px
    return 0, 0.0


def api_token(base: str, user: str, password: str) -> str:
    body = urllib.parse.urlencode({"username": user, "password": password}).encode()
    req = urllib.request.Request(
        f"{base.rstrip('/')}/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    return str(data["access_token"])


def api_get(base: str, token: str, path: str, params: dict[str, Any]) -> Any:
    query = urllib.parse.urlencode(params)
    url = f"{base.rstrip('/')}{path}?{query}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def cache_path(symbol: str, day: str) -> Path:
    return CACHE_DIR / f"{symbol}_{day}.jsonl"


def save_ticks(symbol: str, day: str, rows: list[dict[str, Any]]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = cache_path(symbol, day)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_cached_ticks(symbol: str, day: str) -> list[dict[str, Any]] | None:
    path = cache_path(symbol, day)
    if not path.exists() or path.stat().st_size < 10:
        return None
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def fetch_ticks_window(
    base: str,
    token: str,
    symbol: str,
    start: str,
    end: str,
    limit: int = 20000,
) -> list[dict[str, Any]]:
    raw = api_get(
        base,
        token,
        "/data/tick",
        {
            "symbol": symbol,
            "exchange": "CFFEX",
            "start": start,
            "end": end,
            "limit": limit,
        },
    )
    return raw if isinstance(raw, list) else []


def fetch_day_ticks(
    base: str,
    token: str,
    symbol: str,
    day: str,
    chunk_minutes: int = 5,
) -> list[dict[str, Any]]:
    cached = load_cached_ticks(symbol, day)
    if cached is not None:
        return cached

    sessions = [("09:29:00", "11:30:00"), ("13:00:00", "15:00:00")]
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sess_start, sess_end in sessions:
        cursor = datetime.fromisoformat(f"{day} {sess_start}")
        end_dt = datetime.fromisoformat(f"{day} {sess_end}")
        while cursor < end_dt:
            nxt = min(cursor + timedelta(minutes=chunk_minutes), end_dt)
            try:
                chunk = fetch_ticks_window(
                    base,
                    token,
                    symbol,
                    cursor.strftime("%Y-%m-%d %H:%M:%S"),
                    nxt.strftime("%Y-%m-%d %H:%M:%S"),
                )
            except urllib.error.HTTPError:
                chunk = []
            for tick in chunk:
                key = (
                    f"{tick.get('datetime')}|{tick.get('last_price')}|{tick.get('volume')}|"
                    f"{tick.get('bid_price_1')}|{tick.get('ask_price_1')}"
                )
                if key in seen:
                    continue
                seen.add(key)
                rows.append(tick)
            cursor = nxt
            time.sleep(0.03)

    rows.sort(key=lambda r: str(r.get("datetime") or ""))
    save_ticks(symbol, day, rows)
    return rows


def pick_atm_strike(spot: float) -> float:
    return max(STRIKE_STEP, round_to(spot, STRIKE_STEP))


def option_symbols(expiry: str, strikes: list[float]) -> list[tuple[str, int, float]]:
    out: list[tuple[str, int, float]] = []
    for strike in strikes:
        ks = int(strike)
        out.append((f"{expiry}-C-{ks}", 1, float(ks)))
        out.append((f"{expiry}-P-{ks}", -1, float(ks)))
    return out


def row_to_book(row: dict[str, Any]) -> Book:
    return Book(
        bid=float(row.get("bid_price_1") or 0),
        ask=float(row.get("ask_price_1") or 0),
        bid_vol=float(row.get("bid_volume_1") or 0),
        ask_vol=float(row.get("ask_volume_1") or 0),
        last=float(row.get("last_price") or 0),
        volume=float(row.get("volume") or 0),
        dt=parse_dt(row["datetime"]) if row.get("datetime") else None,
    )


def resolve_trading_days(start: str, end: str) -> list[str]:
    days: list[str] = []
    cur = datetime.fromisoformat(start).date()
    last = datetime.fromisoformat(end).date()
    while cur <= last:
        if cur.weekday() < 5:
            days.append(cur.isoformat())
        cur += timedelta(days=1)
    return days


def resolve_expiry(expiry_code: str) -> date:
    if expiry_code in EXPIRY:
        return EXPIRY[expiry_code]
    yy = 2000 + int(expiry_code[2:4])
    mm = int(expiry_code[4:6])
    d0 = date(yy, mm, 1)
    fridays = [
        d0 + timedelta(days=i)
        for i in range(31)
        if (d0 + timedelta(days=i)).month == mm and (d0 + timedelta(days=i)).weekday() == 4
    ]
    return fridays[2] if len(fridays) >= 3 else d0 + timedelta(days=14)


def prepare_dataset(
    days: list[str],
    underlying: str,
    expiry_code: str,
    atm_strikes: int,
    base: str,
    user: str,
    password: str,
    use_cache_only: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], list[tuple[str, int, float]], dict[str, Any]]:
    token = "" if use_cache_only else api_token(base, user, password)

    under_all: list[dict[str, Any]] = []
    for day in days:
        if use_cache_only:
            rows = load_cached_ticks(underlying, day) or []
        else:
            rows = fetch_day_ticks(base, token, underlying, day)
        under_all.extend(rows)

    if not under_all:
        raise RuntimeError("标的 Tick 为空，请检查日期或缓存")

    mids: list[float] = []
    step = max(1, len(under_all) // 200)
    for row in under_all[::step]:
        bid = float(row.get("bid_price_1") or 0)
        ask = float(row.get("ask_price_1") or 0)
        if bid > 0 and ask > 0:
            mids.append((bid + ask) / 2)
    spot0 = float(np.median(mids)) if mids else float(under_all[-1].get("last_price") or 4500)
    atm = pick_atm_strike(spot0)
    strikes = [atm + i * STRIKE_STEP for i in range(-atm_strikes, atm_strikes + 1)]
    legs_meta = option_symbols(expiry_code, strikes)

    option_ticks: dict[str, list[dict[str, Any]]] = {}
    for sym, _cp, _k in legs_meta:
        bucket: list[dict[str, Any]] = []
        for day in days:
            if use_cache_only:
                rows = load_cached_ticks(sym, day) or []
            else:
                rows = fetch_day_ticks(base, token, sym, day)
            bucket.extend(rows)
        option_ticks[sym] = bucket

    meta = {
        "underlying": underlying,
        "expiry_code": expiry_code,
        "atm": atm,
        "strikes": strikes,
        "symbols": [s for s, _, _ in legs_meta],
        "days": days,
        "under_ticks": len(under_all),
        "option_ticks": {s: len(v) for s, v in option_ticks.items()},
        "spot0": round(spot0, 2),
    }
    return under_all, option_ticks, legs_meta, meta


def run_one(
    underlying_ticks: list[dict[str, Any]],
    option_ticks: dict[str, list[dict[str, Any]]],
    legs_meta: list[tuple[str, int, float]],
    params: Params,
    expiry: date,
) -> dict[str, Any]:
    legs: dict[str, LegState] = {
        sym: LegState(symbol=sym, cp=cp, strike=strike) for sym, cp, strike in legs_meta
    }
    fut_pos = 0
    cash = 0.0
    spot = 0.0

    fills = 0
    buy_fills = 0
    sell_fills = 0
    spread_cap: list[float] = []
    turnover_opt = 0
    turnover_fut = 0
    quote_updates = 0

    equity_curve: list[tuple[str, float]] = []
    daily_pnl: dict[str, float] = {}
    day_start_eq: dict[str, float] = {}
    last_day = ""
    peak = 0.0
    max_dd = 0.0
    last_equity = 0.0
    trade_log: list[dict[str, Any]] = []

    events: list[tuple[datetime, int, str, dict[str, Any]]] = []
    for row in underlying_ticks:
        dt = parse_dt(row["datetime"])
        if in_session(dt):
            events.append((dt, 0, "IF", row))
    for sym, rows in option_ticks.items():
        for row in rows:
            dt = parse_dt(row["datetime"])
            if in_session(dt):
                events.append((dt, 1, sym, row))
    events.sort(key=lambda x: (x[0], x[1], x[2]))

    def mark_equity() -> float:
        eq = cash + fut_pos * spot * FUT_SIZE
        for leg in legs.values():
            mark = leg.mid or leg.theo
            eq += leg.pos * mark * OPT_SIZE
        return eq

    for dt, kind, symbol, row in events:
        day = dt.strftime("%Y-%m-%d")
        if day != last_day:
            if last_day and last_day in day_start_eq:
                daily_pnl[last_day] = round(last_equity - day_start_eq[last_day], 2)
            day_start_eq[day] = last_equity
            last_day = day

        if kind == 0:
            book = row_to_book(row)
            if book.bid > 0 and book.ask > 0:
                spot = (book.bid + book.ask) / 2
            elif book.last > 0:
                spot = book.last
            continue

        if spot <= 0:
            continue

        book = row_to_book(row)
        leg = legs[symbol]

        side, px = try_fill(leg.quote, book, params)
        if side:
            vol = params.quote_volume
            cash -= side * px * OPT_SIZE * vol
            cash -= OPT_COMM * vol
            leg.pos += side * vol
            fills += 1
            turnover_opt += vol
            if side > 0:
                buy_fills += 1
            else:
                sell_fills += 1
            capture = (leg.quote.mid - px) * side * OPT_SIZE
            spread_cap.append(capture)
            trade_log.append(
                {
                    "dt": dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "symbol": symbol,
                    "side": "B" if side > 0 else "S",
                    "px": px,
                    "mid": round(leg.quote.mid, 2),
                    "pos": leg.pos,
                    "capture": round(capture, 2),
                }
            )
            leg.quote = Quote(mid=leg.quote.mid, theo=leg.quote.theo)

        tte = year_fraction(dt.date(), expiry)
        theo, delta, gamma_v, vega = calc_greeks(spot, leg.strike, tte, params.sigma, leg.cp)
        mkt_mid = (book.bid + book.ask) / 2 if book.bid > 0 and book.ask > 0 else book.last
        atm_k = pick_atm_strike(spot)
        _t, _d, atm_g, _v = calc_greeks(spot, atm_k, tte, params.sigma, 1)
        rel_g = abs(gamma_v) / atm_g if atm_g > 0 else 1.0
        leg.theo = theo
        leg.delta = delta
        leg.gamma = gamma_v
        leg.vega = vega
        leg.mid = mkt_mid
        leg.quote = build_quote(
            params,
            theo,
            mkt_mid,
            book.bid,
            book.ask,
            leg.pos,
            delta,
            vega,
            rel_g,
        )
        quote_updates += 1

        if params.hedge and spot > 0:
            port_delta = sum(item.pos * item.delta * OPT_SIZE for item in legs.values())
            target = -port_delta / FUT_SIZE
            diff = target - fut_pos
            if abs(diff) >= params.hedge_lots:
                lots = int(round(diff))
                if lots:
                    cash -= lots * spot * FUT_SIZE
                    cash -= abs(lots) * FUT_COMM
                    fut_pos += lots
                    turnover_fut += abs(lots)

        last_equity = mark_equity()
        peak = max(peak, last_equity)
        max_dd = min(max_dd, last_equity - peak)
        if quote_updates % 500 == 0:
            equity_curve.append((dt.strftime("%Y-%m-%d %H:%M:%S"), round(last_equity, 2)))

    if last_day and last_day in day_start_eq:
        daily_pnl[last_day] = round(last_equity - day_start_eq[last_day], 2)

    flatten_fee = sum(abs(leg.pos) for leg in legs.values()) * OPT_COMM + abs(fut_pos) * FUT_COMM
    final = last_equity - flatten_fee

    pnl_arr = np.array(list(daily_pnl.values()), dtype=float) if daily_pnl else np.zeros(0)
    rets = pnl_arr / CAPITAL
    sharpe = float(np.mean(rets) / (np.std(rets) + 1e-9) * math.sqrt(242)) if len(rets) > 1 else 0.0
    win = float(np.mean(pnl_arr > 0)) if len(pnl_arr) else 0.0

    return {
        "name": params.name,
        "final_pnl": round(final, 2),
        "sharpe": round(sharpe, 3),
        "max_dd": round(max_dd, 2),
        "win_rate": round(win * 100, 1),
        "fills": fills,
        "buy_fills": buy_fills,
        "sell_fills": sell_fills,
        "avg_spread_capture": round(float(np.mean(spread_cap)), 2) if spread_cap else 0.0,
        "median_spread_capture": round(float(np.median(spread_cap)), 2) if spread_cap else 0.0,
        "opt_turnover": turnover_opt,
        "fut_turnover": turnover_fut,
        "quote_updates": quote_updates,
        "end_pos": {sym: leg.pos for sym, leg in legs.items()},
        "end_fut": fut_pos,
        "end_spot": round(spot, 2),
        "daily_pnl": daily_pnl,
        "daily_mean": round(float(np.mean(pnl_arr)), 2) if len(pnl_arr) else 0.0,
        "daily_std": round(float(np.std(pnl_arr)), 2) if len(pnl_arr) else 0.0,
        "best_day": round(float(np.max(pnl_arr)), 2) if len(pnl_arr) else 0.0,
        "worst_day": round(float(np.min(pnl_arr)), 2) if len(pnl_arr) else 0.0,
        "equity_curve": equity_curve[-80:],
        "trades_sample": trade_log[:20]
        + ([{"...": f"共{len(trade_log)}笔"}] if len(trade_log) > 20 else []),
        "trade_count": len(trade_log),
        "hedge": params.hedge,
        "gamma": params.gamma,
        "kappa": params.kappa,
        "params": asdict(params),
    }


def build_report(payload: dict[str, Any]) -> str:
    results = payload.get("results") or []
    sample = payload.get("sample") or {}
    assumptions = payload.get("assumptions") or {}
    lines = [
        "# L1 期权做市回测报告",
        "",
        f"- 生成时间：{payload.get('generated')}",
        f"- 样本：{sample.get('start')} ~ {sample.get('end')}，交易日 {sample.get('days')} 天",
        f"- 标的：{sample.get('underlying')} / 期权链 {sample.get('expiry_code')} "
        f"ATM={sample.get('atm')} 行权价 {sample.get('strikes')}",
        f"- Tick：标的 {sample.get('under_ticks')}，"
        f"期权合计 {sum((sample.get('option_ticks') or {}).values())}",
        f"- 成交模型：{assumptions.get('fill_mode')} @ {assumptions.get('fill_at')}，挂单滞后一拍",
        "",
        "## 参数组对比",
        "",
        "| 名称 | 盈亏 | 夏普 | 最大回撤 | 胜率% | 成交笔数 | 价差捕获均值 | 期权换手 | IF换手 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    best = None
    for row in results:
        lines.append(
            f"| {row['name']} | {row['final_pnl']} | {row['sharpe']} | {row['max_dd']} | "
            f"{row['win_rate']} | {row['fills']} | {row['avg_spread_capture']} | "
            f"{row['opt_turnover']} | {row['fut_turnover']} |"
        )
        if best is None or float(row["final_pnl"]) > float(best["final_pnl"]):
            best = row
    lines.append("")
    if best:
        lines.extend(
            [
                "## 最优组解读（按盈亏）",
                "",
                f"- **{best['name']}**：最终盈亏 {best['final_pnl']}，夏普 {best['sharpe']}，"
                f"最大回撤 {best['max_dd']}",
                f"- 日均盈亏 {best['daily_mean']}（标准差 {best['daily_std']}），"
                f"最好/最差日 {best['best_day']} / {best['worst_day']}",
                f"- 买卖成交 {best['buy_fills']}/{best['sell_fills']}，"
                f"价差捕获均值 {best['avg_spread_capture']}（中位数 {best['median_spread_capture']}）",
                f"- 日盈亏：{json.dumps(best.get('daily_pnl') or {}, ensure_ascii=False)}",
                f"- 期末仓位：期权 {best.get('end_pos')}，IF {best.get('end_fut')}，"
                f"spot {best.get('end_spot')}",
                "",
                "### 含义",
                "",
                "- 价差捕获为正：触价成交整体落在公允价有利一侧。",
                "- 若盈亏为负而捕获为正：多为 Delta/Gamma 存货风险或对冲成本吞噬价差。",
                "- 本回测为 L1 简化模型，未计入队列优先、部分成交、拒单与延迟抖动，实盘会更差。",
                "",
            ]
        )
    lines.extend(
        [
            "## 局限与下一步",
            "",
            "1. 仅 L1；无 L2/逐笔，排队与毒性估计偏乐观。",
            "2. 历史仅约数个完整交易日，统计不稳定。",
            "3. 波动率用固定 sigma，未做实时 IV 校准。",
            "4. 下一步：加 IV 曲面、多档行权价库存联合、更严的成交确认。",
            "",
        ]
    )
    return "\n".join(lines)


def run_backtest(
    start: str = "2026-09-08",
    end: str = "2026-09-11",
    underlying: str = "IF2609",
    expiry_code: str = "IO2609",
    compare: bool = True,
    params: Params | None = None,
    api_base: str = DEFAULT_API,
    user: str = DEFAULT_USER,
    password: str = DEFAULT_PASS,
    use_cache_only: bool = False,
    atm_strikes: int | None = None,
) -> dict[str, Any]:
    days = resolve_trading_days(start, end)
    base_params = params or PRESETS[0]
    n_atm = base_params.atm_strikes if atm_strikes is None else atm_strikes

    under, options, legs_meta, meta = prepare_dataset(
        days,
        underlying,
        expiry_code,
        n_atm,
        api_base,
        user,
        password,
        use_cache_only=use_cache_only,
    )
    expiry = resolve_expiry(expiry_code)
    variants = list(PRESETS) if compare else [base_params]
    results = []
    for item in variants:
        cfg = Params(**{**asdict(item), "atm_strikes": n_atm})
        results.append(run_one(under, options, legs_meta, cfg, expiry))

    payload = {
        "generated": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "universe": f"{underlying} + {expiry_code} ATM L1 Tick AS-MM",
        "interval": "tick",
        "compare": compare,
        "assumptions": {
            "option_size": OPT_SIZE,
            "futures_size": FUT_SIZE,
            "pricetick": PRICETICK,
            "opt_commission": OPT_COMM,
            "fut_commission": FUT_COMM,
            "capital_for_sharpe": CAPITAL,
            "fill_mode": (params or PRESETS[0]).fill_mode,
            "fill_at": (params or PRESETS[0]).fill_at,
            "quote_lag": "1_tick",
            "source": api_base if not use_cache_only else str(CACHE_DIR),
            "sigma_fixed": True,
        },
        "sample": {
            "start": days[0] if days else "",
            "end": days[-1] if days else "",
            "days": len(days),
            **meta,
        },
        "results": results,
    }
    RESULT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_PATH.write_text(build_report(payload), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="L1 IO 期权做市 Tick 回测")
    parser.add_argument("--start", default="2026-09-08")
    parser.add_argument("--end", default="2026-09-11")
    parser.add_argument("--underlying", default="IF2609")
    parser.add_argument("--expiry", default="IO2609")
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", default=DEFAULT_PASS)
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument("--no-compare", action="store_true")
    parser.add_argument("--atm-strikes", type=int, default=0)
    args = parser.parse_args()

    out = run_backtest(
        start=args.start,
        end=args.end,
        underlying=args.underlying,
        expiry_code=args.expiry,
        compare=not args.no_compare,
        api_base=args.api,
        user=args.user,
        password=args.password,
        use_cache_only=args.cache_only,
        atm_strikes=args.atm_strikes,
    )
    summary = {
        row["name"]: {
            "pnl": row["final_pnl"],
            "sharpe": row["sharpe"],
            "dd": row["max_dd"],
            "fills": row["fills"],
            "capture": row["avg_spread_capture"],
        }
        for row in out["results"]
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("saved", RESULT_PATH)
    print("report", REPORT_PATH)


if __name__ == "__main__":
    main()
