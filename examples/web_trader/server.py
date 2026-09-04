"""FastAPI backend for the headless VeighNa Web Trader."""

from __future__ import annotations

import asyncio
import csv
import importlib.util
import inspect
import json
import math
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from jose import JWTError, jwt
from pydantic import BaseModel, Field

from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Direction, Exchange, Interval, Offset, OptionType, OrderType, Product
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import (
    EVENT_ACCOUNT,
    EVENT_CONTRACT,
    EVENT_LOG,
    EVENT_ORDER,
    EVENT_POSITION,
    EVENT_TICK,
    EVENT_TRADE,
)
from vnpy.trader.object import (
    CancelRequest,
    ContractData,
    OrderData,
    OrderRequest,
    SubscribeRequest,
)
from vnpy.trader.database import get_database
from vnpy.trader.utility import get_file_path, get_folder_path, load_json, save_json


def safe_load_json(filename: str) -> dict[str, Any]:
    """Load JSON status/config without crashing on empty or partial writes."""
    try:
        data = load_json(filename)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
from vnpy_ctabacktester.engine import (
    EVENT_BACKTESTER_BACKTESTING_FINISHED,
    EVENT_BACKTESTER_LOG,
    BacktesterEngine,
)
from vnpy_ctastrategy.base import EVENT_CTA_LOG, EVENT_CTA_STRATEGY
from vnpy_ctastrategy.engine import CtaEngine
from vnpy_datamanager.engine import ManagerEngine
from vnpy_datarecorder.engine import EVENT_RECORDER_LOG, EVENT_RECORDER_UPDATE, RecorderEngine
from vnpy_optionmaster.base import (
    EVENT_OPTION_ALGO_LOG,
    EVENT_OPTION_NEW_PORTFOLIO,
    EVENT_OPTION_RISK_NOTICE,
    OptionData,
    get_underlying_prefix,
)
from vnpy_optionmaster.engine import PRICING_MODELS, OptionEngine
from vnpy_optionmaster.time import ANNUAL_DAYS
from vnpy_scripttrader.engine import EVENT_SCRIPT_LOG, ScriptEngine
from vnpy_spreadtrading.base import (
    EVENT_SPREAD_ALGO,
    EVENT_SPREAD_DATA,
    EVENT_SPREAD_LOG,
    EVENT_SPREAD_POS,
    EVENT_SPREAD_STRATEGY,
)
from vnpy_spreadtrading.engine import SpreadEngine

STATIC_DIR = Path(__file__).resolve().parent.joinpath("static")
WEB_SCRIPTS_DIR = Path(__file__).resolve().parent.joinpath("scripts")
SETTING_FILE = "web_trader_setting.json"
CONNECT_FILE_MAP = {"CTP": "connect_ctp.json"}

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7
SECRET_KEY = os.getenv("WEB_SECRET_KEY") or secrets.token_urlsafe(32)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

main_engine: MainEngine | None = None
event_engine: EventEngine | None = None
cta_engine: CtaEngine | None = None
backtester_engine: BacktesterEngine | None = None
data_engine: ManagerEngine | None = None
recorder_engine: RecorderEngine | None = None
option_engine: OptionEngine | None = None
spread_engine: SpreadEngine | None = None
script_engine: ScriptEngine | None = None
_live_supervisor: "LiveSupervisor | None" = None
script_bt_lock = threading.Lock()
script_bt_state: dict[str, Any] = {
    "running": False,
    "phase": "",
    "message": "",
    "error": "",
    "result": None,
    "progress": None,
    "engine": "gex",
    "kind": "SA",
    "interval": "1d",
}
event_loop: asyncio.AbstractEventLoop | None = None

auth_username = "vnpy"
auth_password = "vnpy"
log_buffer: deque[dict[str, Any]] = deque(maxlen=500)
active_websockets: list[WebSocket] = []


def _first_text(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def load_web_setting() -> None:
    """登录账号以 web_trader_setting.json 为准；环境变量仅在文件未配置时用于首次初始化。"""
    global auth_username, auth_password, SECRET_KEY

    setting = load_json(SETTING_FILE)
    if not isinstance(setting, dict):
        setting = {}

    auth_username = _first_text(setting.get("username"), os.getenv("WEB_USERNAME"), "vnpy")
    auth_password = _first_text(setting.get("password"), os.getenv("WEB_PASSWORD"), "vnpy")
    secret = _first_text(setting.get("secret_key"), os.getenv("WEB_SECRET_KEY"), SECRET_KEY)
    SECRET_KEY = secret
    setting["username"] = auth_username
    setting["password"] = auth_password
    setting["secret_key"] = SECRET_KEY
    save_json(SETTING_FILE, setting)


def attach_runtime(
    attached_main: MainEngine,
    attached_event: EventEngine,
    attached_cta: CtaEngine | None,
    attached_backtester: BacktesterEngine | None,
    attached_data: ManagerEngine | None,
    attached_recorder: RecorderEngine,
    attached_option: OptionEngine,
    attached_spread: SpreadEngine | None,
    attached_script: ScriptEngine | None,
) -> None:
    global main_engine, event_engine, cta_engine, backtester_engine, data_engine
    global recorder_engine, option_engine, spread_engine, script_engine
    main_engine = attached_main
    event_engine = attached_event
    cta_engine = attached_cta
    backtester_engine = attached_backtester
    data_engine = attached_data
    recorder_engine = attached_recorder
    option_engine = attached_option
    spread_engine = attached_spread
    script_engine = attached_script
    load_web_setting()
    start_live_supervisor()


def require_main() -> MainEngine:
    if main_engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="交易引擎未启动")
    return main_engine


def require_cta() -> CtaEngine:
    if cta_engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="CTA引擎未启动")
    return cta_engine


def require_backtester() -> BacktesterEngine:
    if backtester_engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="回测引擎未启动")
    return backtester_engine


def require_data() -> ManagerEngine:
    if data_engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="数据管理引擎未启动")
    return data_engine


def require_recorder() -> RecorderEngine:
    if recorder_engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="行情记录引擎未启动")
    return recorder_engine


def require_option() -> OptionEngine:
    if option_engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="期权引擎未启动")
    return option_engine


def apply_option_portfolio_setting(
    engine: OptionEngine,
    portfolio_name: str,
    model_name: str,
    interest_rate: float,
    chain_underlying_map: dict[str, str],
    precision: int,
    margin_setting: dict | None = None,
) -> None:
    """Call OptionEngine.update_portfolio_setting across package versions.

    PyPI 1.2.2 has no margin_setting argument; newer local builds do.
    """
    kwargs: dict[str, Any] = {
        "portfolio_name": portfolio_name,
        "model_name": model_name,
        "interest_rate": interest_rate,
        "chain_underlying_map": chain_underlying_map,
        "precision": precision,
    }
    params = inspect.signature(engine.update_portfolio_setting).parameters
    if "margin_setting" in params:
        kwargs["margin_setting"] = margin_setting
    engine.update_portfolio_setting(**kwargs)


def require_spread() -> SpreadEngine:
    if spread_engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="价差引擎未启动")
    return spread_engine


def require_script() -> ScriptEngine:
    if script_engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="脚本引擎未启动")
    return script_engine


def option_market_tick(option: OptionData | None) -> Any:
    if option is None:
        return None
    tick = getattr(option, "tick", None)
    if tick is None and main_engine is not None:
        tick = main_engine.get_tick(option.vt_symbol)
    return tick


def serialize_option(option: OptionData) -> dict[str, Any]:
    tick = option_market_tick(option)
    return {
        "vt_symbol": option.vt_symbol,
        "strike_price": option.strike_price,
        "chain_index": option.chain_index,
        "option_type": "Call" if option.option_type == 1 else "Put",
        "days_to_expiry": option.days_to_expiry,
        "net_pos": option_net_position(option),
        "open_interest": float(getattr(tick, "open_interest", 0) or 0) if tick else 0,
        "bid_price": getattr(tick, "bid_price_1", 0) if tick else 0,
        "ask_price": getattr(tick, "ask_price_1", 0) if tick else 0,
        "last_price": getattr(tick, "last_price", 0) if tick else 0,
        "mid_impv": round(option.mid_impv * 100, 2) if option.mid_impv else 0,
        "theo_delta": option.theo_delta,
        "theo_gamma": option.theo_gamma,
        "theo_theta": option.theo_theta,
        "theo_vega": option.theo_vega,
        "pos_delta": option.pos_delta,
    }


def option_open_interest(option: OptionData | None) -> float:
    tick = option_market_tick(option)
    return float(getattr(tick, "open_interest", 0) or 0) if tick else 0.0


def option_gex_1pct(option: OptionData | None, spot: float, volume: float) -> float:
    """标的变动 1% 时的 Delta 敞口；theo_gamma 已含合约乘数。"""
    if not option or not spot:
        return 0.0
    return float(option.theo_gamma or 0) * float(volume or 0) * spot * 0.01


def _gex_flip_strike(strikes: list[dict[str, Any]], value_key: str) -> float | None:
    cum = 0.0
    prev_cum = 0.0
    prev_strike = None
    flip = None
    nearest_strike = None
    nearest_abs = None
    for row in strikes:
        prev_cum = cum
        cum += float(row.get(value_key) or 0)
        row[f"cum_{value_key}"] = round(cum, 4)
        abs_cum = abs(cum)
        if nearest_abs is None or abs_cum < nearest_abs:
            nearest_abs = abs_cum
            nearest_strike = row.get("strike")
        if (
            flip is None
            and prev_strike is not None
            and prev_cum * cum <= 0
            and (prev_cum or cum)
        ):
            denom = abs(prev_cum) + abs(cum)
            ratio = abs(prev_cum) / denom if denom else 0
            flip = prev_strike + ratio * (float(row["strike"]) - prev_strike)
        prev_strike = row.get("strike")
    if flip is not None:
        return round(float(flip), 4)
    return round(float(nearest_strike), 4) if nearest_strike is not None else None


def chain_spot_info(chain, chain_symbol: str = "") -> dict[str, Any]:
    underlying = getattr(chain, "underlying", None) if chain else None
    mid = float(getattr(underlying, "mid_price", 0) or 0) if underlying else 0.0
    adj = float(getattr(chain, "underlying_adjustment", 0) or 0) if chain else 0.0
    atm = float(getattr(chain, "atm_price", 0) or 0) if chain else 0.0
    from_mid = mid > 0
    return {
        "chain_symbol": chain_symbol,
        "spot": (mid + adj) if from_mid else atm,
        "from_mid": from_mid,
        "atm_price": atm,
        "days_to_expiry": int(getattr(chain, "days_to_expiry", 0) or 0) if chain else 0,
        "underlying": getattr(underlying, "vt_symbol", "") if underlying else "",
    }


def chain_spot_price(chain) -> float:
    return float(chain_spot_info(chain).get("spot") or 0)


def compute_chain_gex(chain) -> dict[str, Any]:
    info = chain_spot_info(chain)
    underlying = getattr(chain, "underlying", None)
    spot = float(info.get("spot") or 0)
    atm_price = float(info.get("atm_price") or 0)

    strikes: list[dict[str, Any]] = []
    call_gex_sum = 0.0
    put_gex_sum = 0.0
    pos_gex_sum = 0.0
    call_oi_sum = 0.0
    put_oi_sum = 0.0

    for index in chain.indexes:
        call = chain.calls.get(index)
        put = chain.puts.get(index)
        option = call or put
        try:
            strike = float(option.strike_price) if option and option.strike_price else float(index)
        except (TypeError, ValueError):
            strike = 0.0
        call_oi = option_open_interest(call)
        put_oi = option_open_interest(put)
        call_pos = option_net_position(call)
        put_pos = option_net_position(put)
        call_gex = option_gex_1pct(call, spot, call_oi)
        put_gex = -option_gex_1pct(put, spot, put_oi)
        call_pos_gex = option_gex_1pct(call, spot, call_pos)
        put_pos_gex = -option_gex_1pct(put, spot, put_pos)
        pos_gex = call_pos_gex + put_pos_gex
        net_gex = call_gex + put_gex
        call_gex_sum += call_gex
        put_gex_sum += put_gex
        pos_gex_sum += pos_gex
        call_oi_sum += call_oi
        put_oi_sum += put_oi
        strikes.append(
            {
                "index": index,
                "strike": strike,
                "call_oi": call_oi,
                "put_oi": put_oi,
                "call_gamma": float(getattr(call, "theo_gamma", 0) or 0) if call else 0.0,
                "put_gamma": float(getattr(put, "theo_gamma", 0) or 0) if put else 0.0,
                "call_gex": round(call_gex, 4),
                "put_gex": round(put_gex, 4),
                "net_gex": round(net_gex, 4),
                "pos_gex": round(pos_gex, 4),
                "call_pos_gex": round(call_pos_gex, 4),
                "put_pos_gex": round(put_pos_gex, 4),
                "call_pos": call_pos,
                "put_pos": put_pos,
            }
        )

    flip_strike = _gex_flip_strike(strikes, "net_gex")
    call_wall = max(strikes, key=lambda row: row["call_gex"], default=None)
    put_wall = min(strikes, key=lambda row: row["put_gex"], default=None)
    has_pos = any(row["call_pos"] or row["put_pos"] for row in strikes)
    if has_pos:
        pos_flip_strike = _gex_flip_strike(strikes, "pos_gex")
        pos_call_wall = max(strikes, key=lambda row: row["call_pos_gex"], default=None)
        pos_put_wall = min(strikes, key=lambda row: row["put_pos_gex"], default=None)
    else:
        pos_flip_strike = None
        pos_call_wall = None
        pos_put_wall = None
    pin = max(strikes, key=lambda row: row["call_oi"] + row["put_oi"], default=None)
    max_gamma = max(strikes, key=lambda row: abs(row["net_gex"]), default=None)

    return {
        "spot": round(spot, 4) if spot else 0.0,
        "spot_from_mid": bool(info.get("from_mid")),
        "atm_price": atm_price,
        "atm_index": getattr(chain, "atm_index", "") or "",
        "underlying": getattr(underlying, "vt_symbol", "") if underlying else "",
        "days_to_expiry": getattr(chain, "days_to_expiry", 0) or 0,
        "call_gex": round(call_gex_sum, 4),
        "put_gex": round(put_gex_sum, 4),
        "net_gex": round(call_gex_sum + put_gex_sum, 4),
        "pos_gex": round(pos_gex_sum, 4),
        "call_pos_gex": round(sum(row["call_pos_gex"] for row in strikes), 4),
        "put_pos_gex": round(sum(row["put_pos_gex"] for row in strikes), 4),
        "call_pos": round(sum(row["call_pos"] for row in strikes), 4),
        "put_pos": round(sum(row["put_pos"] for row in strikes), 4),
        "pos_lots": round(sum(abs(row["call_pos"]) + abs(row["put_pos"]) for row in strikes), 4),
        "call_oi": call_oi_sum,
        "put_oi": put_oi_sum,
        "flip_strike": flip_strike,
        "pos_flip_strike": pos_flip_strike,
        "call_wall": call_wall["strike"] if call_wall else None,
        "put_wall": put_wall["strike"] if put_wall else None,
        "pos_call_wall": pos_call_wall["strike"] if pos_call_wall else None,
        "pos_put_wall": pos_put_wall["strike"] if pos_put_wall else None,
        "pin": pin["strike"] if pin else None,
        "max_gamma_strike": max_gamma["strike"] if max_gamma else None,
        "strikes": strikes,
        "convention": "dealer",
        "unit": "delta_1pct",
    }


def _pick_reference_spot(candidates: list[dict[str, Any]], preferred_chain: str = "") -> dict[str, Any] | None:
    if not candidates:
        return None
    preferred = preferred_chain.strip()
    if preferred:
        for item in candidates:
            if item.get("chain_symbol") == preferred:
                return item
    mid_only = [item for item in candidates if item.get("from_mid")]
    pool = mid_only or candidates
    return min(pool, key=lambda item: (int(item.get("days_to_expiry") or 10**9), str(item.get("chain_symbol") or "")))


def compute_gex_stack(portfolio, preferred_chain: str = "") -> dict[str, Any]:
    """按行权价对齐、按到期月拆开的 GEX，供堆积柱状图使用。"""
    month_maps: list[dict[str, Any]] = []
    strike_set: set[float] = set()
    spot_candidates: list[dict[str, Any]] = []
    for chain_symbol in portfolio_chain_symbols(portfolio):
        chain = get_portfolio_chain(portfolio, chain_symbol)
        if not chain:
            continue
        payload = compute_chain_gex(chain)
        spot_val = float(payload.get("spot") or 0)
        if spot_val:
            spot_candidates.append(
                {
                    "chain_symbol": chain_symbol,
                    "spot": spot_val,
                    "from_mid": bool(payload.get("spot_from_mid")),
                    "days_to_expiry": int(payload.get("days_to_expiry") or 0),
                    "underlying": str(payload.get("underlying") or ""),
                }
            )
        by_strike: dict[float, dict[str, Any]] = {}
        for row in payload.get("strikes") or []:
            strike = float(row.get("strike") or 0)
            if strike <= 0:
                continue
            strike_set.add(strike)
            by_strike[strike] = row
        if not by_strike:
            continue
        month_maps.append(
            {
                "chain_symbol": chain_symbol,
                "label": chain_symbol.split(".")[0],
                "days_to_expiry": int(payload.get("days_to_expiry") or 0),
                "spot": spot_val,
                "spot_from_mid": bool(payload.get("spot_from_mid")),
                "by_strike": by_strike,
            }
        )
    month_maps.sort(key=lambda item: (item["days_to_expiry"], item["label"]))
    picked = _pick_reference_spot(spot_candidates, preferred_chain)
    spot = float(picked["spot"]) if picked else 0.0
    underlying = str(picked["underlying"]) if picked else ""
    strikes = sorted(strike_set)
    months: list[dict[str, Any]] = []
    keys = ("net_gex", "pos_gex", "call_gex", "put_gex", "call_pos_gex", "put_pos_gex", "call_oi", "put_oi", "call_pos", "put_pos")
    for item in month_maps:
        series: dict[str, list[float]] = {key: [] for key in keys}
        for strike in strikes:
            row = item["by_strike"].get(strike) or {}
            for key in keys:
                series[key].append(round(float(row.get(key) or 0), 4))
        months.append(
            {
                "chain_symbol": item["chain_symbol"],
                "label": item["label"],
                "days_to_expiry": item["days_to_expiry"],
                "spot": item.get("spot") or 0,
                "spot_from_mid": bool(item.get("spot_from_mid")),
                **series,
            }
        )
    agg: list[dict[str, Any]] = []
    for index, strike in enumerate(strikes):
        row = {"strike": strike}
        for key in keys:
            row[key] = round(sum(float(month[key][index]) for month in months), 4)
        agg.append(row)
    has_pos = any(row["call_pos"] or row["put_pos"] for row in agg)
    flip = _gex_flip_strike([{**row} for row in agg], "net_gex")
    call_wall = max(agg, key=lambda row: row["call_gex"], default=None) if agg else None
    put_wall = min(agg, key=lambda row: row["put_gex"], default=None) if agg else None
    pin = max(agg, key=lambda row: row["call_oi"] + row["put_oi"], default=None) if agg else None
    if has_pos:
        pos_flip = _gex_flip_strike([{**row} for row in agg], "pos_gex")
        pos_call_wall = max(agg, key=lambda row: row["call_pos_gex"], default=None)
        pos_put_wall = min(agg, key=lambda row: row["put_pos_gex"], default=None)
    else:
        pos_flip = None
        pos_call_wall = None
        pos_put_wall = None
    return {
        "spot": spot,
        "spot_from_mid": bool(picked and picked.get("from_mid")),
        "spot_chain": str(picked["chain_symbol"]) if picked else "",
        "underlying": underlying,
        "strikes": strikes,
        "months": months,
        "net_gex": round(sum(row["net_gex"] for row in agg), 4) if agg else 0.0,
        "pos_gex": round(sum(row["pos_gex"] for row in agg), 4) if agg else 0.0,
        "flip_strike": flip,
        "pos_flip_strike": pos_flip,
        "call_wall": call_wall["strike"] if call_wall else None,
        "put_wall": put_wall["strike"] if put_wall else None,
        "pos_call_wall": pos_call_wall["strike"] if pos_call_wall else None,
        "pos_put_wall": pos_put_wall["strike"] if pos_put_wall else None,
        "pin": pin["strike"] if pin else None,
        "has_pos": has_pos,
    }


def option_mid_price(option: OptionData | None) -> float:
    tick = getattr(option, "tick", None) if option else None
    if not tick:
        return 0.0
    bid = float(getattr(tick, "bid_price_1", 0) or 0)
    ask = float(getattr(tick, "ask_price_1", 0) or 0)
    last = float(getattr(tick, "last_price", 0) or 0)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return bid or ask or last


def option_margin(option: OptionData, spot: float, mid: float) -> float:
    try:
        option.calculate_theo_margin()
    except Exception:
        pass
    margin = float(getattr(option, "theo_margin", 0) or 0)
    if margin > 0:
        return margin
    if not spot or mid <= 0:
        return 0.0
    size = float(getattr(option, "size", 1) or 1)
    strike = float(option.strike_price or 0)
    if option.option_type > 0:
        otm = max(strike - spot, 0.0)
    else:
        otm = max(spot - strike, 0.0)
    extra = max(spot * 0.12 - otm, spot * 0.07)
    return (mid + extra) * size


def option_tv_yield_point(option: OptionData | None, spot: float) -> dict[str, Any] | None:
    if not option or not spot:
        return None
    mid = option_mid_price(option)
    if mid <= 0:
        return None
    try:
        strike = float(option.strike_price or 0)
    except (TypeError, ValueError):
        return None
    if option.option_type > 0:
        intrinsic = max(0.0, spot - strike)
    else:
        intrinsic = max(0.0, strike - spot)
    size = float(getattr(option, "size", 1) or 1)
    time_value = max(0.0, mid - intrinsic) * size
    margin = option_margin(option, spot, mid)
    dte = int(getattr(option, "days_to_expiry", 0) or 0)
    if margin <= 0 or dte <= 0:
        return None
    annual = time_value / margin * (ANNUAL_DAYS / dte) * 100.0
    if annual < 0 or annual > 2000:
        return None
    return {
        "strike": strike,
        "yield": round(annual, 2),
        "time_value": round(time_value, 2),
        "margin": round(margin, 2),
        "option_type": "Call" if option.option_type > 0 else "Put",
        "days_to_expiry": dte,
    }


def compute_tv_yield(portfolio, preferred_chain: str = "") -> dict[str, Any]:
    series: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for chain_symbol in portfolio_chain_symbols(portfolio):
        chain = get_portfolio_chain(portfolio, chain_symbol)
        if not chain:
            continue
        info = chain_spot_info(chain, chain_symbol)
        cspot = float(info.get("spot") or 0)
        if cspot:
            candidates.append(info)
        calls: list[dict[str, Any]] = []
        puts: list[dict[str, Any]] = []
        for index in chain.indexes:
            call_point = option_tv_yield_point(chain.calls.get(index), cspot)
            put_point = option_tv_yield_point(chain.puts.get(index), cspot)
            if call_point:
                calls.append(call_point)
            if put_point:
                puts.append(put_point)
        if not calls and not puts:
            continue
        calls.sort(key=lambda item: item["strike"])
        puts.sort(key=lambda item: item["strike"])
        dte = int(
            getattr(chain, "days_to_expiry", 0)
            or (calls[0]["days_to_expiry"] if calls else puts[0]["days_to_expiry"])
        )
        series.append(
            {
                "chain_symbol": chain_symbol,
                "label": chain_symbol.split(".")[0],
                "days_to_expiry": dte,
                "calls": calls,
                "puts": puts,
                "points": calls + puts,
            }
        )
    series.sort(key=lambda item: item["days_to_expiry"])
    picked = _pick_reference_spot(candidates, preferred_chain)
    spot = float(picked["spot"]) if picked else 0.0
    return {
        "spot": round(spot, 4) if spot else 0,
        "spot_chain": str(picked["chain_symbol"]) if picked else "",
        "annual_days": ANNUAL_DAYS,
        "series": series,
    }


def option_iv_point(option) -> dict[str, Any] | None:
    if not option:
        return None
    try:
        strike = float(option.strike_price or 0)
    except (TypeError, ValueError):
        return None
    iv = float(getattr(option, "mid_impv", 0) or 0) * 100.0
    if strike <= 0 or iv < 0.5 or iv > 300:
        return None
    return {
        "strike": strike,
        "iv": round(iv, 2),
        "option_type": "Call" if option.option_type > 0 else "Put",
        "days_to_expiry": int(getattr(option, "days_to_expiry", 0) or 0),
    }


def compute_iv_smile(portfolio, preferred_chain: str = "") -> dict[str, Any]:
    series: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for chain_symbol in portfolio_chain_symbols(portfolio):
        chain = get_portfolio_chain(portfolio, chain_symbol)
        if not chain:
            continue
        info = chain_spot_info(chain, chain_symbol)
        if info.get("spot"):
            candidates.append(info)
        calls: list[dict[str, Any]] = []
        puts: list[dict[str, Any]] = []
        for index in chain.indexes:
            call_point = option_iv_point(chain.calls.get(index))
            put_point = option_iv_point(chain.puts.get(index))
            if call_point:
                calls.append(call_point)
            if put_point:
                puts.append(put_point)
        if not calls and not puts:
            continue
        calls.sort(key=lambda item: item["strike"])
        puts.sort(key=lambda item: item["strike"])
        dte = int(
            getattr(chain, "days_to_expiry", 0)
            or (calls[0]["days_to_expiry"] if calls else puts[0]["days_to_expiry"])
        )
        series.append(
            {
                "chain_symbol": chain_symbol,
                "label": chain_symbol.split(".")[0],
                "days_to_expiry": dte,
                "calls": calls,
                "puts": puts,
                "points": calls + puts,
            }
        )
    series.sort(key=lambda item: item["days_to_expiry"])
    picked = _pick_reference_spot(candidates, preferred_chain)
    spot = float(picked["spot"]) if picked else 0.0
    return {
        "spot": round(spot, 4) if spot else 0,
        "spot_chain": str(picked["chain_symbol"]) if picked else "",
        "series": series,
    }


def portfolio_chain_symbols(portfolio) -> list[str]:
    symbols = set(getattr(portfolio, "_chains", {}) or {})
    symbols.update(getattr(portfolio, "chains", {}) or {})
    return sorted(symbols)


def get_portfolio_chain(portfolio, chain_symbol: str):
    return (portfolio.chains.get(chain_symbol) if portfolio.chains else None) or (
        portfolio._chains.get(chain_symbol) if getattr(portfolio, "_chains", None) else None
    )


def guess_underlying(engine: OptionEngine, portfolio_name: str, chain_symbol: str, saved_map: dict[str, str]) -> str:
    saved = str((saved_map or {}).get(chain_symbol, "")).strip()
    if saved:
        if "LOCAL" in saved or engine.main_engine.get_contract(saved):
            return saved
    chain_code = chain_symbol.split(".")[0]
    exch = chain_symbol.split(".", 1)[-1] if "." in chain_symbol else "CFFEX"
    product = portfolio_name.split(".")[0]
    prefix = get_underlying_prefix(portfolio_name)
    month = chain_code[len(product):] if chain_code.upper().startswith(product.upper()) else ""
    if prefix and month:
        want = f"{prefix}{month}.{exch}"
        if engine.main_engine.get_contract(want):
            return want
    candidates = engine.get_underlying_symbols(portfolio_name)
    if prefix and month:
        want_code = f"{prefix}{month}"
        for vt_symbol in candidates:
            if vt_symbol.split(".")[0] == want_code:
                return vt_symbol
    for vt_symbol in candidates:
        if vt_symbol.split(".")[0] == chain_code or vt_symbol == chain_symbol:
            return vt_symbol
    for vt_symbol in candidates:
        if vt_symbol.startswith(chain_code):
            return vt_symbol
    if candidates:
        return candidates[0]
    return f"{chain_code}.LOCAL"


def build_live_chain_map(engine: OptionEngine, portfolio_name: str) -> dict[str, str]:
    portfolio = engine.get_portfolio(portfolio_name)
    saved = engine.get_portfolio_setting(portfolio_name).get("chain_underlying_map") or {}
    return {
        chain_symbol: guess_underlying(engine, portfolio_name, chain_symbol, saved)
        for chain_symbol in portfolio_chain_symbols(portfolio)
    }


def script_dir() -> Path:
    folder = get_folder_path("scripts")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def list_script_files() -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    skip_prefixes = ("backtest_", "fetch_")
    for folder in (WEB_SCRIPTS_DIR, script_dir()):
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.py")):
            name = path.name
            if name.startswith("_") or name.startswith(skip_prefixes):
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            files.append(str(path))
    return files


def resolve_script_path(raw: str) -> Path:
    text = (raw or "").strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="请选择脚本")
    path = Path(text)
    name = path.name if path.suffix.lower() == ".py" else f"{path.name}.py"
    candidates = []
    if path.suffix.lower() == ".py":
        candidates.append(path)
    candidates.append(WEB_SCRIPTS_DIR.joinpath(name))
    candidates.append(script_dir().joinpath(name))
    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.suffix.lower() == ".py":
                return candidate
        except OSError:
            continue
    raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到脚本 {name}")


def load_web_script(name: str) -> Any:
    path = WEB_SCRIPTS_DIR.joinpath(f"{name}.py")
    if not path.exists():
        raise FileNotFoundError(f"找不到脚本 {name}.py")
    scripts_dir = str(WEB_SCRIPTS_DIR)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    key = f"web_trader_{name}"
    existing = sys.modules.get(key)
    if existing is not None and getattr(existing, "__file__", None) == str(path):
        return existing
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(key, None)
        raise
    return module


GEX_CACHE_SCRIPTS = {
    ("SA", "1d"): ["fetch_sa_daily"],
    ("SA", "5m"): ["fetch_sa_5min"],
    ("SA", "30m"): ["fetch_sa_30min"],
    ("IF", "1d"): ["fetch_if_daily"],
    ("IF", "5m"): ["fetch_if_5min"],
    ("IF", "30m"): ["fetch_if_5min", "fetch_if_30min"],
}


def _norm_script_engine(value: str | None) -> str:
    token = str(value or "gex").lower()
    if token in ("as", "as_mm", "mm", "option_mm"):
        return "as_mm"
    return "gex"


def _norm_script_kind(value: str | None) -> str:
    return "IF" if str(value or "SA").upper() == "IF" else "SA"


def _norm_script_interval(value: str | None) -> str:
    raw = str(value or "1d").lower().strip()
    if raw in ("1d", "d", "day", "daily", "1day"):
        return "1d"
    if raw in ("30", "30m", "30min", "30minute"):
        return "30m"
    if raw in ("5", "5m", "5min", "5minute"):
        return "5m"
    return "1d"


def script_backtest_snapshot(
    engine: str | None = None,
    kind: str | None = None,
    interval: str | None = None,
) -> dict[str, Any]:
    with script_bt_lock:
        running = bool(script_bt_state["running"])
        phase = script_bt_state["phase"]
        message = script_bt_state["message"]
        error = script_bt_state["error"]
        live = script_bt_state["result"]
        progress = script_bt_state.get("progress")
        state_engine = script_bt_state.get("engine") or "gex"
        state_kind = script_bt_state.get("kind") or "SA"
        state_interval = script_bt_state.get("interval") or "1d"

    engine = _norm_script_engine(engine or state_engine)
    kind = _norm_script_kind(kind or state_kind)
    interval = _norm_script_interval(interval or state_interval)
    try:
        if engine == "gex":
            module = load_web_script("backtest_gex_tv_strangle")
            module.configure(kind, interval)
            cache = module.cache_meta()
            presets = module.preset_payloads()
            saved = module.load_saved_result()
        else:
            module = load_web_script("backtest_as_option_mm")
            cache = module.cache_meta()
            presets = module.preset_payloads()
            saved = module.load_saved_result()
    except Exception as exc:
        cache = {"exists": False, "error": str(exc)}
        presets = []
        saved = None

    result = saved
    if isinstance(live, dict):
        live_engine = _norm_script_engine(live.get("engine") or state_engine)
        if engine == "gex":
            live_kind = _norm_script_kind(live.get("kind") or state_kind)
            live_interval = _norm_script_interval(live.get("interval") or state_interval)
            if live_engine == "gex" and live_kind == kind and live_interval == interval:
                result = live
        elif live_engine == "as_mm":
            result = live

    return {
        "running": running,
        "phase": phase,
        "message": message,
        "error": error,
        "result": result,
        "progress": progress,
        "cache": cache,
        "presets": presets,
        "engine": engine,
        "kind": kind,
        "interval": interval,
    }


def run_script_backtest_job(payload: dict[str, Any]) -> None:
    engine = _norm_script_engine(payload.get("engine"))
    kind = _norm_script_kind(payload.get("kind"))
    interval = _norm_script_interval(payload.get("interval"))
    try:
        compare = bool(payload.get("compare"))
        with script_bt_lock:
            script_bt_state["engine"] = engine
            script_bt_state["kind"] = kind
            script_bt_state["interval"] = interval
            script_bt_state["message"] = "正在回测…"
            script_bt_state["progress"] = None
        if engine == "gex":
            module = load_web_script("backtest_gex_tv_strangle")
            module.configure(kind, interval)
            meta = module.cache_meta()
            if not meta.get("exists"):
                raise RuntimeError(
                    meta.get("error")
                    or f"缺少 {kind} {interval} 行情缓存，请先点击「刷新行情缓存」"
                )
            if compare:
                params = None
            else:
                payload = dict(payload)
                payload["hedge"] = False
                params = module.params_from_dict(payload)
            result = module.run_backtest(params=params, compare=compare, kind=kind, interval=interval)
        else:
            module = load_web_script("backtest_as_option_mm")
            seed = int(payload.get("seed") or 42)
            params = None if compare else module.params_from_dict(payload)
            result = module.run_backtest(params=params, compare=compare, seed=seed)
            if isinstance(result, dict):
                result["engine"] = "as_mm"
        with script_bt_lock:
            script_bt_state["result"] = result
            script_bt_state["message"] = "回测完成"
            script_bt_state["error"] = ""
    except Exception as exc:
        with script_bt_lock:
            script_bt_state["error"] = str(exc)
            script_bt_state["message"] = "回测失败"
    finally:
        with script_bt_lock:
            script_bt_state["running"] = False
            script_bt_state["phase"] = ""


def run_script_cache_job(payload: dict[str, Any] | None = None) -> None:
    payload = payload or {}
    engine = _norm_script_engine(payload.get("engine"))
    kind = _norm_script_kind(payload.get("kind"))
    interval = _norm_script_interval(payload.get("interval"))
    try:
        with script_bt_lock:
            script_bt_state["engine"] = engine
            script_bt_state["kind"] = kind
            script_bt_state["interval"] = interval
        if engine == "gex":
            scripts = GEX_CACHE_SCRIPTS.get((kind, interval)) or ["fetch_sa_daily"]
            labels = {"1d": "日线", "30m": "30分钟", "5m": "5分钟"}
            for name in scripts:
                with script_bt_lock:
                    script_bt_state["message"] = f"正在刷新 {kind} {labels.get(interval, interval)} 缓存（{name}）…"
                load_web_script(name).main()
            with script_bt_lock:
                script_bt_state["message"] = f"{kind} {labels.get(interval, interval)} 缓存已更新"
                script_bt_state["error"] = ""
        else:
            with script_bt_lock:
                script_bt_state["message"] = "正在拉取沪深300 30分钟行情…"
            load_web_script("fetch_csi300_30min").main()
            with script_bt_lock:
                script_bt_state["message"] = "行情缓存已更新"
                script_bt_state["error"] = ""
    except Exception as exc:
        with script_bt_lock:
            script_bt_state["error"] = str(exc)
            script_bt_state["message"] = "刷新缓存失败"
    finally:
        with script_bt_lock:
            script_bt_state["running"] = False
            script_bt_state["phase"] = ""


def run_script_optimize_job(payload: dict[str, Any]) -> None:
    def on_progress(info: dict[str, Any]) -> None:
        with script_bt_lock:
            script_bt_state["message"] = str(info.get("message") or "正在寻优…")
            script_bt_state["progress"] = {
                "done": info.get("done") or 0,
                "total": info.get("total") or 0,
                "best": ((info.get("best") or {}).get("name") or ""),
                "best_score": (info.get("best") or {}).get("score"),
            }
            if info.get("result"):
                script_bt_state["result"] = info["result"]

    try:
        module = load_web_script("backtest_as_option_mm")
        base = module.params_from_dict(payload)
        with script_bt_lock:
            script_bt_state["message"] = "正在寻优…"
            script_bt_state["progress"] = {"done": 0, "total": 0}
        result = module.run_optimize(
            base,
            gamma=(payload["gamma_start"], payload["gamma_end"], payload["gamma_step"]),
            kappa=(payload["kappa_start"], payload["kappa_end"], payload["kappa_step"]),
            spread_mult=(payload["spread_start"], payload["spread_end"], payload["spread_step"]),
            tau_days=(payload["tau_start"], payload["tau_end"], payload["tau_step"]),
            hedge_mode=str(payload.get("hedge_mode") or "on"),
            objective=str(payload.get("objective") or "sharpe"),
            seed=int(payload.get("seed") or 42),
            on_progress=on_progress,
        )
        with script_bt_lock:
            script_bt_state["result"] = result
            script_bt_state["message"] = "寻优完成"
            script_bt_state["error"] = ""
            optimize = result.get("optimize") or {}
            script_bt_state["progress"] = {
                "done": optimize.get("combos") or 0,
                "total": optimize.get("combos") or 0,
                "best": optimize.get("best_name") or "",
                "best_score": optimize.get("best_score"),
            }
    except Exception as exc:
        with script_bt_lock:
            script_bt_state["error"] = str(exc)
            script_bt_state["message"] = "寻优失败"
    finally:
        with script_bt_lock:
            script_bt_state["running"] = False
            script_bt_state["phase"] = ""


def to_plain(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, str):
        return value
    if hasattr(value, "item") and callable(value.item):
        try:
            return to_plain(value.item())
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(key): to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_plain(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            key: to_plain(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)


def parse_datetime(text: str) -> datetime:
    text = text.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"无法解析时间: {text}")


def is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return "密" in key or lowered in {"password", "passwd", "token", "授权编码"}


def mask_setting(setting: dict[str, Any]) -> dict[str, Any]:
    masked: dict[str, Any] = {}
    for key, value in setting.items():
        if is_secret_key(str(key)):
            masked[key] = ""
        elif isinstance(value, list):
            masked[key] = value[0] if value else ""
        else:
            masked[key] = value
    return masked


def load_saved_gateway_setting(gateway_name: str) -> dict[str, Any]:
    """Load CTP/gateway connect JSON.

    STANDALONE_RECORDER may prefer connect_ctp_recorder.json (second MD account)
    to avoid kicking the web trader session on the same investor id.
    """
    override = (os.getenv("CTP_CONNECT_FILE") or "").strip()
    if override:
        candidates = [override]
    elif gateway_name.upper() == "CTP" and env_flag("STANDALONE_RECORDER"):
        candidates = ["connect_ctp_recorder.json", "connect_ctp.json"]
    else:
        candidates = [CONNECT_FILE_MAP.get(gateway_name, f"connect_{gateway_name.lower()}.json")]
    for filename in candidates:
        filepath = get_file_path(filename)
        if not filepath.exists():
            continue
        data = load_json(filename)
        if isinstance(data, dict) and data:
            return data
    return {}


def save_gateway_setting(gateway_name: str, setting: dict[str, Any]) -> None:
    filename = CONNECT_FILE_MAP.get(gateway_name, f"connect_{gateway_name.lower()}.json")
    save_json(filename, setting)


def create_access_token(username: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": username, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def decode_username(token: str) -> str:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="令牌无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    username = payload.get("sub")
    if not username or not secrets.compare_digest(str(username), auth_username):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="令牌无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return str(username)


async def get_access(token: str = Depends(oauth2_scheme)) -> bool:
    decode_username(token)
    return True


_tick_ws_last: float = 0.0
TICK_WS_INTERVAL = 0.25


def compact_tick(tick: Any) -> dict[str, Any]:
    return {
        "vt_symbol": getattr(tick, "vt_symbol", ""),
        "last_price": getattr(tick, "last_price", 0),
        "bid_price_1": getattr(tick, "bid_price_1", 0),
        "ask_price_1": getattr(tick, "ask_price_1", 0),
        "volume": getattr(tick, "volume", 0),
        "open_interest": getattr(tick, "open_interest", 0),
    }


def register_events() -> None:
    if event_engine is None:
        return

    topics = [
        EVENT_TICK,
        EVENT_ORDER,
        EVENT_TRADE,
        EVENT_POSITION,
        EVENT_ACCOUNT,
        EVENT_CONTRACT,
        EVENT_LOG,
        EVENT_CTA_LOG,
        EVENT_CTA_STRATEGY,
        EVENT_BACKTESTER_LOG,
        EVENT_BACKTESTER_BACKTESTING_FINISHED,
        EVENT_OPTION_NEW_PORTFOLIO,
        EVENT_OPTION_ALGO_LOG,
        EVENT_OPTION_RISK_NOTICE,
        EVENT_SPREAD_DATA,
        EVENT_SPREAD_POS,
        EVENT_SPREAD_ALGO,
        EVENT_SPREAD_LOG,
        EVENT_SPREAD_STRATEGY,
        EVENT_SCRIPT_LOG,
        EVENT_RECORDER_LOG,
        EVENT_RECORDER_UPDATE,
    ]
    for topic in topics:
        event_engine.register(topic, process_event)


def process_event(event: Event) -> None:
    global _tick_ws_last

    if event.type == EVENT_TICK:
        if not active_websockets or event_loop is None:
            return
        now = time.monotonic()
        if now - _tick_ws_last < TICK_WS_INTERVAL:
            return
        _tick_ws_last = now
        payload = {"topic": event.type, "data": compact_tick(event.data)}
    elif event.type in {
        EVENT_LOG,
        EVENT_CTA_LOG,
        EVENT_BACKTESTER_LOG,
        EVENT_SPREAD_LOG,
        EVENT_SCRIPT_LOG,
        EVENT_OPTION_ALGO_LOG,
        EVENT_RECORDER_LOG,
    }:
        msg = ""
        event_time = datetime.now().isoformat(sep=" ", timespec="seconds")
        data = event.data
        if isinstance(data, str):
            msg = data
        elif data is not None:
            msg = str(getattr(data, "msg", data))
            event_time = str(getattr(data, "time", event_time))
        log_buffer.append({"topic": event.type, "time": event_time, "msg": msg})
        payload = {"topic": event.type, "data": {"time": event_time, "msg": msg}}
    else:
        payload = {"topic": event.type, "data": to_plain(event.data)}

    if not active_websockets or event_loop is None:
        return

    try:
        message = json.dumps(payload, ensure_ascii=False)
    except TypeError:
        payload["data"] = str(event.data)
        message = json.dumps(payload, ensure_ascii=False)
    asyncio.run_coroutine_threadsafe(websocket_broadcast(message), event_loop)


async def websocket_broadcast(msg: str) -> None:
    stale: list[WebSocket] = []
    for websocket in active_websockets:
        try:
            await websocket.send_text(msg)
        except Exception:
            stale.append(websocket)
    for websocket in stale:
        if websocket in active_websockets:
            active_websockets.remove(websocket)


app = FastAPI(title="VeighNa Web Trader", version="1.0.0")


@app.on_event("startup")
async def on_startup() -> None:
    global event_loop
    event_loop = asyncio.get_running_loop()
    register_events()

    def _web_heartbeat_loop() -> None:
        while True:
            try:
                from system_monitor import write_service_heartbeat

                write_service_heartbeat(
                    "web",
                    {
                        "role": "web",
                        "md_source": (os.getenv("LIVE_MD_SOURCE") or "ctp").lower(),
                        "skip_md": os.getenv("LIVE_CTP_SKIP_MD", "0"),
                    },
                )
            except Exception:
                pass
            time.sleep(10)

    threading.Thread(target=_web_heartbeat_loop, name="web-sys-heartbeat", daemon=True).start()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(
        STATIC_DIR.joinpath("index.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@app.post("/token")
def login(form_data: OAuth2PasswordRequestForm = Depends()) -> dict[str, str]:
    load_web_setting()
    user_ok = secrets.compare_digest(form_data.username, auth_username)
    pass_ok = secrets.compare_digest(form_data.password, auth_password)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {"access_token": create_access_token(auth_username), "token_type": "bearer"}


@app.get("/meta")
def get_meta(_: bool = Depends(get_access)) -> dict[str, Any]:
    return {
        "exchanges": [item.value for item in Exchange],
        "intervals": [item.value for item in Interval],
        "directions": [item.value for item in Direction],
        "offsets": [item.value for item in Offset],
        "order_types": [item.value for item in OrderType],
        "option_models": list(PRICING_MODELS.keys()),
    }


@app.get("/gateway")
def get_gateways(_: bool = Depends(get_access)) -> dict[str, Any]:
    engine = require_main()
    gateways = []
    for name in engine.get_all_gateway_names():
        default_setting = engine.get_default_setting(name) or {}
        saved = load_saved_gateway_setting(name)
        choices: dict[str, list[Any]] = {}
        fields: dict[str, Any] = {}
        for key, value in default_setting.items():
            if isinstance(value, list):
                choices[key] = value
                fields[key] = saved.get(key, value[0] if value else "")
            else:
                fields[key] = saved.get(key, value)
        password_saved = any(is_secret_key(str(key)) and saved.get(key) for key in saved)
        gateways.append(
            {
                "name": name,
                "fields": mask_setting(fields),
                "choices": choices,
                "password_saved": password_saved,
            }
        )
    return {"gateways": gateways}


class ConnectModel(BaseModel):
    gateway_name: str
    setting: dict[str, Any] = Field(default_factory=dict)


@app.post("/gateway/connect")
def connect_gateway(model: ConnectModel, _: bool = Depends(get_access)) -> dict[str, str]:
    engine = require_main()
    if model.gateway_name not in engine.get_all_gateway_names():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到接口 {model.gateway_name}")

    saved = load_saved_gateway_setting(model.gateway_name)
    merged = dict(saved)
    for key, value in model.setting.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        merged[key] = value.strip() if isinstance(value, str) else value
    default_setting = engine.get_default_setting(model.gateway_name) or {}
    for key, default in default_setting.items():
        if merged.get(key) not in (None, ""):
            continue
        if isinstance(default, list):
            merged[key] = default[0] if default else ""
        else:
            merged.setdefault(key, default)
    missing = [
        str(key)
        for key, default in default_setting.items()
        if not isinstance(default, list) and not merged.get(key)
    ]
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"请完善 {model.gateway_name} 配置（空字段不会覆盖已保存项）: {', '.join(missing)}",
        )
    save_gateway_setting(model.gateway_name, merged)
    engine.connect(merged, model.gateway_name)
    return {"message": f"正在连接 {model.gateway_name}"}


@app.get("/log")
def get_logs(_: bool = Depends(get_access)) -> list[dict[str, Any]]:
    return list(log_buffer)


@app.post("/tick/{vt_symbol}")
def subscribe(vt_symbol: str, _: bool = Depends(get_access)) -> dict[str, str]:
    engine = require_main()
    contract: ContractData | None = engine.get_contract(vt_symbol)
    if not contract:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到合约 {vt_symbol}")
    req = SubscribeRequest(symbol=contract.symbol, exchange=contract.exchange)
    engine.subscribe(req, contract.gateway_name)
    return {"message": f"已订阅 {vt_symbol}"}


@app.get("/tick")
def get_ticks(_: bool = Depends(get_access)) -> list[Any]:
    return to_plain(require_main().get_all_ticks())


class OrderRequestModel(BaseModel):
    symbol: str
    exchange: Exchange
    direction: Direction
    type: OrderType
    volume: float
    price: float = 0
    offset: Offset = Offset.NONE
    reference: str = "WebTrader"


@app.post("/order")
def send_order(model: OrderRequestModel, _: bool = Depends(get_access)) -> dict[str, str]:
    engine = require_main()
    req = OrderRequest(**model.model_dump())
    contract = engine.get_contract(req.vt_symbol)
    if not contract:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"找不到合约 {req.symbol} {req.exchange.value}",
        )
    vt_orderid = engine.send_order(req, contract.gateway_name)
    return {"vt_orderid": vt_orderid}


@app.delete("/order/{vt_orderid}")
def cancel_order(vt_orderid: str, _: bool = Depends(get_access)) -> dict[str, str]:
    engine = require_main()
    order: OrderData | None = engine.get_order(vt_orderid)
    if not order:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到委托 {vt_orderid}")
    req: CancelRequest = order.create_cancel_request()
    engine.cancel_order(req, order.gateway_name)
    return {"message": f"已发出撤单 {vt_orderid}"}


@app.get("/order")
def get_orders(_: bool = Depends(get_access)) -> list[Any]:
    return to_plain(require_main().get_all_orders())


@app.get("/trade")
def get_trades(_: bool = Depends(get_access)) -> list[Any]:
    return to_plain(require_main().get_all_trades())


@app.get("/position")
def get_positions(_: bool = Depends(get_access)) -> list[Any]:
    return to_plain(require_main().get_all_positions())


@app.get("/account")
def get_accounts(_: bool = Depends(get_access)) -> list[Any]:
    return to_plain(require_main().get_all_accounts())


@app.get("/contract")
def get_contracts(_: bool = Depends(get_access)) -> list[Any]:
    return to_plain(require_main().get_all_contracts())


FUTURES_SYMBOL_RE = re.compile(r"^([A-Za-z]+)(\d{3,4})$")


def parse_futures_contract(symbol: str) -> tuple[str, str, int] | None:
    match = FUTURES_SYMBOL_RE.fullmatch(symbol or "")
    if not match:
        return None
    product, month = match.group(1), match.group(2)
    try:
        if len(month) == 4:
            year = 2000 + int(month[:2])
            mm = int(month[2:])
        else:
            year = 2020 + int(month[0])
            mm = int(month[1:])
            now = datetime.now()
            if mm >= 1 and datetime(year, mm, 1) < datetime(now.year - 1, now.month, 1):
                year += 10
        if not 1 <= mm <= 12:
            return None
    except ValueError:
        return None
    return product, month, year * 100 + mm


def tick_last_price(tick) -> float:
    if not tick:
        return 0.0
    last = float(getattr(tick, "last_price", 0) or 0)
    if last:
        return last
    bid = float(getattr(tick, "bid_price_1", 0) or 0)
    ask = float(getattr(tick, "ask_price_1", 0) or 0)
    if bid and ask:
        return (bid + ask) / 2
    return bid or ask


def account_net_pos(engine: MainEngine, vt_symbol: str, symbol: str = "") -> float:
    net = 0.0
    matched = False
    for pos in engine.get_all_positions():
        if pos.vt_symbol != vt_symbol and not (symbol and pos.symbol == symbol):
            continue
        matched = True
        volume = float(pos.volume or 0)
        if pos.direction == Direction.SHORT:
            net -= volume
        else:
            net += volume
    return net if matched else 0.0


def option_net_position(option) -> float:
    """账户净仓：优先 OffsetConverter / 持仓列表，再回退 OptionMaster 缓存。"""
    if not option:
        return 0.0
    vt_symbol = getattr(option, "vt_symbol", "") or ""
    symbol = getattr(option, "symbol", "") or ""
    cached = float(getattr(option, "net_pos", 0) or 0)
    try:
        main = require_main()
    except Exception:
        return cached
    contract = main.get_contract(vt_symbol) if vt_symbol else None
    if contract:
        try:
            converter = main.get_converter(contract.gateway_name)
            holding = converter.get_position_holding(vt_symbol) if converter else None
            if holding:
                net = float(holding.long_pos or 0) - float(holding.short_pos or 0)
                if net:
                    option.long_pos = holding.long_pos
                    option.short_pos = holding.short_pos
                    option.net_pos = int(net)
                    return net
        except Exception:
            pass
    net = account_net_pos(main, vt_symbol, symbol)
    if net:
        option.net_pos = int(net)
        return net
    return cached


def group_futures_products(engine: MainEngine) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for contract in engine.get_all_contracts():
        if contract.product != Product.FUTURES:
            continue
        parsed = parse_futures_contract(contract.symbol)
        if not parsed:
            continue
        product, month, yyyymm = parsed
        key = f"{product}.{contract.exchange.value}"
        group = groups.get(key)
        if not group:
            name = (contract.name or "").replace(month, "").strip(" -_") or product
            group = {
                "key": key,
                "product": product,
                "exchange": contract.exchange.value,
                "name": name,
                "contracts": [],
            }
            groups[key] = group
        group["contracts"].append((yyyymm, month, contract))
    for group in groups.values():
        group["contracts"].sort(key=lambda item: item[0])
    return groups


def build_futures_curve(engine: MainEngine, product_key: str) -> dict[str, Any]:
    groups = group_futures_products(engine)
    group = groups.get(product_key)
    if not group:
        return {
            "product_key": product_key,
            "product": "",
            "exchange": "",
            "name": "",
            "front": "",
            "dominant": "",
            "total_oi": 0,
            "structure": 0,
            "structure_label": "—",
            "rows": [],
            "capital": {"months": [], "option_contracts": 0, "totals": {}},
        }

    rows: list[dict[str, Any]] = []
    for yyyymm, month, contract in group["contracts"]:
        tick = engine.get_tick(contract.vt_symbol)
        price = tick_last_price(tick)
        oi = float(getattr(tick, "open_interest", 0) or 0) if tick else 0.0
        volume = float(getattr(tick, "volume", 0) or 0) if tick else 0.0
        pre_close = float(getattr(tick, "pre_close", 0) or 0) if tick else 0.0
        change = (price - pre_close) if price and pre_close else 0.0
        rows.append(
            {
                "vt_symbol": contract.vt_symbol,
                "symbol": contract.symbol,
                "name": contract.name,
                "month": month,
                "yyyymm": yyyymm,
                "price": round(price, 8) if price else 0,
                "pre_close": pre_close,
                "change": round(change, 8) if change else 0,
                "open_interest": oi,
                "volume": volume,
                "size": float(contract.size or 0),
                "notional": round(oi * price * float(contract.size or 0), 2),
                "account_pos": account_net_pos(engine, contract.vt_symbol),
            }
        )

    priced = [row for row in rows if row["price"]]
    front = priced[0] if priced else (rows[0] if rows else None)
    dominant = max(rows, key=lambda row: row["open_interest"]) if rows else None
    if dominant and not dominant["open_interest"] and priced:
        dominant = max(priced, key=lambda row: row["volume"])
    front_price = front["price"] if front else 0
    dominant_price = dominant["price"] if dominant else 0
    total_oi = sum(row["open_interest"] for row in rows)

    prev_price = 0.0
    for row in rows:
        row["oi_ratio"] = round(row["open_interest"] / total_oi * 100, 2) if total_oi else 0
        row["vs_front"] = round(row["price"] - front_price, 8) if row["price"] and front_price else 0
        row["vs_front_pct"] = round(row["vs_front"] / front_price * 100, 4) if row["price"] and front_price else 0
        row["vs_dominant"] = round(row["price"] - dominant_price, 8) if row["price"] and dominant_price else 0
        row["spread"] = round(row["price"] - prev_price, 8) if row["price"] and prev_price else 0
        row["is_front"] = bool(front and row["vt_symbol"] == front["vt_symbol"])
        row["is_dominant"] = bool(dominant and row["vt_symbol"] == dominant["vt_symbol"])
        if row["price"]:
            prev_price = row["price"]

    far = priced[-1] if priced else None
    structure = (far["price"] - front["price"]) if far and front and far["price"] and front["price"] else 0
    spreads = [row["spread"] for row in rows if row["spread"]]
    pos_n = sum(1 for item in spreads if item > 0)
    neg_n = sum(1 for item in spreads if item < 0)
    if pos_n and neg_n:
        structure_label = "混合"
    elif pos_n:
        structure_label = "升水"
    elif neg_n:
        structure_label = "贴水"
    elif structure > 0:
        structure_label = "升水"
    elif structure < 0:
        structure_label = "贴水"
    else:
        structure_label = "—"

    return {
        "product_key": product_key,
        "product": group["product"],
        "exchange": group["exchange"],
        "name": group["name"],
        "front": front["vt_symbol"] if front else "",
        "dominant": dominant["vt_symbol"] if dominant else "",
        "total_oi": total_oi,
        "structure": round(structure, 8) if structure else 0,
        "structure_label": structure_label,
        "rows": rows,
        "capital": build_futures_capital(engine, group, rows),
    }


RELATED_OPTION_PRODUCTS = {
    "IF": {"IO"},
    "IH": {"HO"},
    "IM": {"MO"},
}


def parse_product_month_from_text(text: str) -> tuple[str, str, int] | None:
    match = re.match(r"^([A-Za-z]+)(\d{3,4})", text or "")
    if not match:
        return None
    return parse_futures_contract(f"{match.group(1)}{match.group(2)}")


def related_option_products(product: str) -> set[str]:
    names = {product, product.lower(), product.upper()}
    extras = RELATED_OPTION_PRODUCTS.get(product.upper(), set())
    names.update(extras)
    names.update(item.lower() for item in extras)
    return names


def normalize_option_portfolio(token: str) -> str:
    text = (token or "").strip()
    if text.lower().endswith("_o"):
        text = text[:-2]
    return text


def option_belongs_to_futures(option: ContractData, product: str, exchange: str) -> bool:
    if option.product != Product.OPTION:
        return False
    if option.exchange.value != exchange:
        return False
    related = {item.lower() for item in related_option_products(product)}
    parsed = parse_product_month_from_text(option.option_underlying or "") or parse_product_month_from_text(
        option.symbol
    )
    if parsed and parsed[0].lower() in related:
        return True
    portfolio = normalize_option_portfolio(option.option_portfolio or "").lower()
    return bool(portfolio) and portfolio in related


def collect_related_options(engine: MainEngine, product: str, exchange: str) -> list[ContractData]:
    return [
        contract
        for contract in engine.get_all_contracts()
        if option_belongs_to_futures(contract, product, exchange)
    ]


def is_option_call(contract: ContractData) -> bool | None:
    if contract.option_type == OptionType.CALL:
        return True
    if contract.option_type == OptionType.PUT:
        return False
    symbol = (contract.symbol or "").upper()
    if "-P-" in symbol or re.search(r"\dP\d", symbol):
        return False
    if "-C-" in symbol or re.search(r"\dC\d", symbol):
        return True
    return None


def new_capital_month(month: str, yyyymm: int) -> dict[str, Any]:
    return {
        "month": month,
        "yyyymm": yyyymm,
        "futures_symbol": "",
        "futures_oi": 0.0,
        "futures_price": 0.0,
        "futures_notional": 0.0,
        "call_oi": 0.0,
        "put_oi": 0.0,
        "call_count": 0,
        "put_count": 0,
        "call_premium": 0.0,
        "put_premium": 0.0,
        "call_notional": 0.0,
        "put_notional": 0.0,
        "strikes": {},
    }


def build_futures_capital(engine: MainEngine, group: dict[str, Any], fut_rows: list[dict[str, Any]]) -> dict[str, Any]:
    product = group["product"]
    exchange = group["exchange"]
    buckets: dict[int, dict[str, Any]] = {}

    def bucket_for(month: str, yyyymm: int) -> dict[str, Any]:
        item = buckets.get(yyyymm)
        if not item:
            item = new_capital_month(month, yyyymm)
            buckets[yyyymm] = item
        return item

    und_price: dict[int, float] = {}
    for row in fut_rows:
        item = bucket_for(row["month"], row["yyyymm"])
        item["futures_symbol"] = row["vt_symbol"]
        item["futures_oi"] = row["open_interest"]
        item["futures_price"] = row["price"]
        item["futures_notional"] = row.get("notional") or 0
        und_price[row["yyyymm"]] = row["price"]

    option_contracts = collect_related_options(engine, product, exchange)
    for option in option_contracts:
        parsed = parse_product_month_from_text(option.option_underlying or "") or parse_product_month_from_text(
            option.symbol
        )
        if not parsed:
            continue
        _opt_product, month, yyyymm = parsed
        call = is_option_call(option)
        if call is None:
            continue
        tick = engine.get_tick(option.vt_symbol)
        oi = float(getattr(tick, "open_interest", 0) or 0) if tick else 0.0
        premium = tick_last_price(tick)
        size = float(option.size or 0)
        underlying = und_price.get(yyyymm, 0.0)
        if not underlying and option.option_underlying:
            underlying = tick_last_price(engine.get_tick(f"{option.option_underlying}.{exchange}"))
        premium_lock = oi * premium * size
        notional = oi * underlying * size
        item = bucket_for(month, yyyymm)
        strike = float(option.option_strike or 0)
        strike_row = item["strikes"].setdefault(
            strike,
            {
                "strike": strike,
                "call_oi": 0.0,
                "put_oi": 0.0,
                "call_premium": 0.0,
                "put_premium": 0.0,
                "call_notional": 0.0,
                "put_notional": 0.0,
                "call_symbol": "",
                "put_symbol": "",
            },
        )
        if call:
            item["call_oi"] += oi
            item["call_count"] += 1
            item["call_premium"] += premium_lock
            item["call_notional"] += notional
            strike_row["call_oi"] += oi
            strike_row["call_premium"] += premium_lock
            strike_row["call_notional"] += notional
            strike_row["call_symbol"] = option.vt_symbol
        else:
            item["put_oi"] += oi
            item["put_count"] += 1
            item["put_premium"] += premium_lock
            item["put_notional"] += notional
            strike_row["put_oi"] += oi
            strike_row["put_premium"] += premium_lock
            strike_row["put_notional"] += notional
            strike_row["put_symbol"] = option.vt_symbol

    months = []
    for yyyymm in sorted(buckets):
        item = buckets[yyyymm]
        item["call_premium"] = round(item["call_premium"], 2)
        item["put_premium"] = round(item["put_premium"], 2)
        item["call_notional"] = round(item["call_notional"], 2)
        item["put_notional"] = round(item["put_notional"], 2)
        item["futures_notional"] = round(item["futures_notional"], 2)
        item["option_premium"] = round(item["call_premium"] + item["put_premium"], 2)
        item["option_notional"] = round(item["call_notional"] + item["put_notional"], 2)
        item["total_notional"] = round(item["futures_notional"] + item["option_notional"], 2)
        item["total_premium"] = round(item["futures_notional"] + item["option_premium"], 2)
        item["strikes"] = [item["strikes"][key] for key in sorted(item["strikes"])]
        for strike_row in item["strikes"]:
            for field in ("call_premium", "put_premium", "call_notional", "put_notional"):
                strike_row[field] = round(strike_row[field], 2)
        months.append(item)

    futures_notional = sum(item["futures_notional"] for item in months)
    call_premium = sum(item["call_premium"] for item in months)
    put_premium = sum(item["put_premium"] for item in months)
    call_notional = sum(item["call_notional"] for item in months)
    put_notional = sum(item["put_notional"] for item in months)
    return {
        "months": months,
        "option_contracts": len(option_contracts),
        "totals": {
            "futures_notional": round(futures_notional, 2),
            "call_premium": round(call_premium, 2),
            "put_premium": round(put_premium, 2),
            "option_premium": round(call_premium + put_premium, 2),
            "call_notional": round(call_notional, 2),
            "put_notional": round(put_notional, 2),
            "option_notional": round(call_notional + put_notional, 2),
            "total_notional": round(futures_notional + call_notional + put_notional, 2),
        },
    }


@app.get("/futures/products")
def list_futures_products(_: bool = Depends(get_access)) -> list[dict[str, Any]]:
    groups = group_futures_products(require_main())
    items = []
    for key in sorted(groups, key=str.lower):
        group = groups[key]
        items.append(
            {
                "key": key,
                "product": group["product"],
                "exchange": group["exchange"],
                "name": group["name"],
                "months": len(group["contracts"]),
            }
        )
    return items


@app.post("/futures/subscribe")
def subscribe_futures_product(
    product_key: str = Query(...),
    include_options: bool = Query(True),
    _: bool = Depends(get_access),
) -> dict[str, Any]:
    engine = require_main()
    groups = group_futures_products(engine)
    group = groups.get(product_key)
    if not group:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到品种 {product_key}")
    count = 0
    for _yyyymm, _month, contract in group["contracts"]:
        engine.subscribe(
            SubscribeRequest(symbol=contract.symbol, exchange=contract.exchange),
            contract.gateway_name,
        )
        count += 1
    option_count = 0
    if include_options:
        for contract in collect_related_options(engine, group["product"], group["exchange"]):
            engine.subscribe(
                SubscribeRequest(symbol=contract.symbol, exchange=contract.exchange),
                contract.gateway_name,
            )
            option_count += 1
    message = f"已订阅 {product_key} {count} 个期货"
    if include_options:
        message += f" + {option_count} 个期权"
    return {"message": message, "count": count, "option_count": option_count}


@app.get("/futures/curve")
def get_futures_curve(
    product_key: str,
    _: bool = Depends(get_access),
) -> dict[str, Any]:
    return build_futures_curve(require_main(), product_key)


@app.get("/cta/class")
def get_cta_classes(_: bool = Depends(get_access)) -> list[str]:
    return require_cta().get_all_strategy_class_names()


@app.get("/cta/class/{class_name}")
def get_cta_class_parameters(class_name: str, _: bool = Depends(get_access)) -> dict[str, Any]:
    engine = require_cta()
    if class_name not in engine.classes:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到策略类 {class_name}")
    return to_plain(engine.get_strategy_class_parameters(class_name))


@app.get("/cta/strategy")
def get_cta_strategies(_: bool = Depends(get_access)) -> list[Any]:
    engine = require_cta()
    return [to_plain(strategy.get_data()) for strategy in engine.strategies.values()]


class AddStrategyModel(BaseModel):
    class_name: str
    strategy_name: str
    vt_symbol: str
    setting: dict[str, Any] = Field(default_factory=dict)


@app.post("/cta/strategy")
def add_cta_strategy(model: AddStrategyModel, _: bool = Depends(get_access)) -> dict[str, str]:
    require_cta().add_strategy(model.class_name, model.strategy_name, model.vt_symbol, model.setting)
    return {"message": f"已创建策略 {model.strategy_name}"}


@app.post("/cta/strategy/{strategy_name}/init")
def init_cta_strategy(strategy_name: str, _: bool = Depends(get_access)) -> dict[str, str]:
    engine = require_cta()
    if strategy_name not in engine.strategies:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到策略 {strategy_name}")
    engine.init_strategy(strategy_name)
    return {"message": f"正在初始化 {strategy_name}"}


@app.post("/cta/strategy/{strategy_name}/start")
def start_cta_strategy(strategy_name: str, _: bool = Depends(get_access)) -> dict[str, str]:
    engine = require_cta()
    if strategy_name not in engine.strategies:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到策略 {strategy_name}")
    engine.start_strategy(strategy_name)
    return {"message": f"已启动 {strategy_name}"}


@app.post("/cta/strategy/{strategy_name}/stop")
def stop_cta_strategy(strategy_name: str, _: bool = Depends(get_access)) -> dict[str, str]:
    engine = require_cta()
    if strategy_name not in engine.strategies:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到策略 {strategy_name}")
    engine.stop_strategy(strategy_name)
    return {"message": f"已停止 {strategy_name}"}


class EditStrategyModel(BaseModel):
    setting: dict[str, Any] = Field(default_factory=dict)


@app.put("/cta/strategy/{strategy_name}")
def edit_cta_strategy(strategy_name: str, model: EditStrategyModel, _: bool = Depends(get_access)) -> dict[str, str]:
    engine = require_cta()
    if strategy_name not in engine.strategies:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到策略 {strategy_name}")
    engine.edit_strategy(strategy_name, model.setting)
    return {"message": f"已更新 {strategy_name}"}


@app.delete("/cta/strategy/{strategy_name}")
def remove_cta_strategy(strategy_name: str, _: bool = Depends(get_access)) -> dict[str, str]:
    engine = require_cta()
    if strategy_name not in engine.strategies:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到策略 {strategy_name}")
    ok = engine.remove_strategy(strategy_name)
    if not ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="策略移除失败，请先停止")
    return {"message": f"已移除 {strategy_name}"}


@app.get("/backtest/class")
def get_backtest_classes(_: bool = Depends(get_access)) -> list[str]:
    return require_backtester().get_strategy_class_names()


@app.get("/backtest/class/{class_name}")
def get_backtest_class_parameters(class_name: str, _: bool = Depends(get_access)) -> dict[str, Any]:
    engine = require_backtester()
    if class_name not in engine.classes:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到策略类 {class_name}")
    return to_plain(engine.get_default_setting(class_name))


class BacktestModel(BaseModel):
    class_name: str
    vt_symbol: str
    interval: str = "1m"
    start: str
    end: str
    rate: float = 0.0001
    slippage: float = 0
    size: int = 10
    pricetick: float = 1
    capital: int = 1_000_000
    setting: dict[str, Any] = Field(default_factory=dict)


@app.post("/backtest")
def start_backtest(model: BacktestModel, _: bool = Depends(get_access)) -> dict[str, Any]:
    engine = require_backtester()
    started = engine.start_backtesting(
        model.class_name,
        model.vt_symbol,
        model.interval,
        parse_datetime(model.start),
        parse_datetime(model.end),
        model.rate,
        model.slippage,
        model.size,
        model.pricetick,
        model.capital,
        model.setting,
    )
    if not started:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="已有回测任务在运行")
    return {"message": "回测已开始"}


@app.get("/backtest/result")
def get_backtest_result(_: bool = Depends(get_access)) -> dict[str, Any]:
    engine = require_backtester()
    return {"statistics": to_plain(engine.get_result_statistics() or {})}


@app.get("/backtest/trade")
def get_backtest_trades(_: bool = Depends(get_access)) -> list[Any]:
    return to_plain(require_backtester().get_all_trades())


@app.get("/backtest/order")
def get_backtest_orders(_: bool = Depends(get_access)) -> list[Any]:
    return to_plain(require_backtester().get_all_orders())


@app.get("/backtest/daily")
def get_backtest_daily(_: bool = Depends(get_access)) -> list[Any]:
    return to_plain(require_backtester().get_all_daily_results())


@app.get("/data/overview")
def get_data_overview(_: bool = Depends(get_access)) -> list[Any]:
    return to_plain(require_data().get_bar_overview())


class DownloadBarModel(BaseModel):
    symbol: str
    exchange: Exchange
    interval: str = "1m"
    start: str


@app.post("/data/download")
def download_bars(model: DownloadBarModel, _: bool = Depends(get_access)) -> dict[str, Any]:
    engine = require_data()
    logs: list[str] = []
    count = engine.download_bar_data(
        model.symbol,
        model.exchange,
        model.interval,
        parse_datetime(model.start),
        logs.append,
    )
    return {"count": count, "logs": logs}


@app.get("/data/bar")
def query_bars(
    symbol: str,
    exchange: Exchange,
    interval: Interval,
    start: str,
    end: str,
    _: bool = Depends(get_access),
) -> list[Any]:
    bars = require_data().load_bar_data(
        symbol,
        exchange,
        interval,
        parse_datetime(start),
        parse_datetime(end),
    )
    return to_plain(bars[-500:])


@app.delete("/data/bar")
def delete_bars(
    symbol: str,
    exchange: Exchange,
    interval: Interval,
    _: bool = Depends(get_access),
) -> dict[str, Any]:
    count = require_data().delete_bar_data(symbol, exchange, interval)
    return {"count": count}


@app.post("/data/import")
async def import_bars(
    file: UploadFile = File(...),
    symbol: str = Form(...),
    exchange: Exchange = Form(...),
    interval: Interval = Form(...),
    tz_name: str = Form("Asia/Shanghai"),
    datetime_head: str = Form("datetime"),
    open_head: str = Form("open"),
    high_head: str = Form("high"),
    low_head: str = Form("low"),
    close_head: str = Form("close"),
    volume_head: str = Form("volume"),
    turnover_head: str = Form("turnover"),
    open_interest_head: str = Form("open_interest"),
    datetime_format: str = Form("%Y-%m-%d %H:%M:%S"),
    _: bool = Depends(get_access),
) -> dict[str, Any]:
    suffix = Path(file.filename or "data.csv").suffix or ".csv"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        start, end, count = require_data().import_data_from_csv(
            tmp_path,
            symbol,
            exchange,
            interval,
            tz_name,
            datetime_head,
            open_head,
            high_head,
            low_head,
            close_head,
            volume_head,
            turnover_head,
            open_interest_head,
            datetime_format,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return {"count": count, "start": to_plain(start), "end": to_plain(end)}


@app.get("/data/export")
def export_bars(
    symbol: str,
    exchange: Exchange,
    interval: Interval,
    start: str,
    end: str,
    _: bool = Depends(get_access),
) -> FileResponse:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
        tmp_path = tmp.name
    ok = require_data().output_data_to_csv(
        tmp_path,
        symbol,
        exchange,
        interval,
        parse_datetime(start),
        parse_datetime(end),
    )
    if not ok:
        Path(tmp_path).unlink(missing_ok=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="导出失败")
    filename = f"{symbol}_{exchange.value}_{interval.value}.csv"
    return FileResponse(tmp_path, filename=filename, media_type="text/csv")


@app.get("/data/tick/overview")
def get_tick_overview(_: bool = Depends(get_access)) -> list[Any]:
    return to_plain(get_database().get_tick_overview())


@app.get("/data/tick")
def query_ticks(
    symbol: str,
    exchange: Exchange,
    start: str,
    end: str,
    limit: int = Query(2000, ge=1, le=20000),
    _: bool = Depends(get_access),
) -> list[Any]:
    ticks = get_database().load_tick_data(
        symbol,
        exchange,
        parse_datetime(start),
        parse_datetime(end),
    )
    if len(ticks) > limit:
        ticks = ticks[-limit:]
    return to_plain(ticks)


@app.delete("/data/tick")
def delete_ticks(
    symbol: str,
    exchange: Exchange,
    _: bool = Depends(get_access),
) -> dict[str, Any]:
    count = get_database().delete_tick_data(symbol, exchange)
    return {"count": count}


@app.get("/data/tick/export")
def export_ticks(
    symbol: str,
    exchange: Exchange,
    start: str,
    end: str,
    _: bool = Depends(get_access),
) -> FileResponse:
    ticks = get_database().load_tick_data(
        symbol,
        exchange,
        parse_datetime(start),
        parse_datetime(end),
    )
    if not ticks:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="没有可导出的 Tick")
    fields = [
        "datetime",
        "symbol",
        "exchange",
        "name",
        "last_price",
        "last_volume",
        "volume",
        "turnover",
        "open_interest",
        "open_price",
        "high_price",
        "low_price",
        "pre_close",
        "limit_up",
        "limit_down",
        "bid_price_1",
        "bid_volume_1",
        "ask_price_1",
        "ask_volume_1",
        "bid_price_2",
        "bid_volume_2",
        "ask_price_2",
        "ask_volume_2",
        "bid_price_3",
        "bid_volume_3",
        "ask_price_3",
        "ask_volume_3",
        "bid_price_4",
        "bid_volume_4",
        "ask_price_4",
        "ask_volume_4",
        "bid_price_5",
        "bid_volume_5",
        "ask_price_5",
        "ask_volume_5",
        "localtime",
    ]
    with tempfile.NamedTemporaryFile(delete=False, suffix=".csv", mode="w", encoding="utf-8", newline="") as tmp:
        tmp_path = tmp.name
        writer = csv.DictWriter(tmp, fieldnames=fields)
        writer.writeheader()
        for tick in ticks:
            row = {key: to_plain(getattr(tick, key, None)) for key in fields}
            row["exchange"] = exchange.value
            writer.writerow(row)
    filename = f"{symbol}_{exchange.value}_tick.csv"
    return FileResponse(tmp_path, filename=filename, media_type="text/csv")


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def should_auto_record_ticks() -> bool:
    """Explicit LIVE_RECORD_TICKS wins; otherwise default on with live iron condor."""
    raw = os.getenv("LIVE_RECORD_TICKS")
    if raw is not None and str(raw).strip() != "":
        return env_flag("LIVE_RECORD_TICKS")
    return env_flag("LIVE_IRON_CONDOR")


def live_portfolios_from_env() -> list[str]:
    return [
        item.strip()
        for item in (os.getenv("LIVE_PORTFOLIOS") or "IO.CFFEX").split(",")
        if item.strip()
    ]


def chain_record_sort_key(chain_symbol: str, chain: Any = None) -> tuple[int, str]:
    dte = getattr(chain, "days_to_expiry", None) if chain is not None else None
    if dte is not None:
        try:
            return (int(dte), chain_symbol)
        except (TypeError, ValueError):
            pass
    code = chain_symbol.split(".")[0]
    match = re.search(r"(\d{4})$", code)
    return (int(match.group(1)) if match else 999999, chain_symbol)


def record_max_chains_from_env() -> int:
    """0 / negative = all option chains; positive = nearest N months."""
    return env_int("LIVE_RECORD_MAX_CHAINS", 0)


def record_filter_window_from_env() -> int:
    """Seconds of allowed |tick.datetime - now| before DataRecorder drops ticks.

    Default 3600: CTP MD can lag under full-chain subscribe load; the stock
    recorder default of 60s silently discards everything once lag exceeds 1m.
    """
    return max(60, env_int("LIVE_RECORD_FILTER_WINDOW", 3600))


def md_max_lag_from_env() -> int:
    """Force CTP reconnect when newest IF/IO tick lags wall clock by this many seconds."""
    return max(60, env_int("LIVE_MD_MAX_LAG_SEC", 90))


def record_scope_label(max_chains: int) -> str:
    if max_chains is None or max_chains <= 0:
        return "全部到期月"
    return f"近{max_chains}个到期月"


def apply_recorder_filter_window(window: int | None = None) -> int:
    """Keep DataRecorder filter aligned with LIVE_RECORD_FILTER_WINDOW."""
    engine = require_recorder()
    value = record_filter_window_from_env() if window is None else max(60, int(window))
    engine.filter_window = value
    engine.filter_delta = timedelta(seconds=value)
    # Refresh baseline so a long lag after restart does not keep rejecting ticks
    # until the next EVENT_TIMER arrives.
    try:
        from vnpy.trader.database import DB_TZ

        engine.filter_dt = datetime.now(DB_TZ)
    except Exception:
        engine.filter_dt = datetime.now()
    return value


def newest_io_if_tick() -> Any | None:
    """Newest in-memory tick among IF/IO symbols (for lag diagnostics)."""
    if main_engine is None:
        return None
    newest = None
    for tick in main_engine.get_all_ticks() or []:
        symbol = (getattr(tick, "symbol", "") or "").upper()
        if not (symbol.startswith("IF") or symbol.startswith("IO")):
            continue
        dt = getattr(tick, "datetime", None)
        if dt is None:
            continue
        if newest is None or dt > getattr(newest, "datetime", dt):
            newest = tick
    return newest


def market_data_lag_sec() -> float | None:
    tick = newest_io_if_tick()
    if tick is None or getattr(tick, "datetime", None) is None:
        return None
    dt = tick.datetime
    now = datetime.now(dt.tzinfo) if getattr(dt, "tzinfo", None) else datetime.now()
    lag = (now - dt).total_seconds()
    # Cached / overnight Redis warmup ticks are not live MD.
    if lag > 3600:
        return None
    return lag


def portfolio_tick_universe(portfolio_name: str, max_chains: int | None = None) -> list[str]:
    """IF 标的 + IO 期权链（可限制近月），供高频回测 Tick/Bar 录制。"""
    engine = require_option()
    portfolio = engine.portfolios.get(portfolio_name)
    if not portfolio:
        return []
    ranked = sorted(
        (
            (chain_record_sort_key(chain_symbol, get_portfolio_chain(portfolio, chain_symbol)), chain_symbol)
            for chain_symbol in portfolio_chain_symbols(portfolio)
        ),
        key=lambda item: item[0],
    )
    if max_chains is not None and max_chains > 0:
        ranked = ranked[:max_chains]
    symbols: list[str] = []
    seen: set[str] = set()
    for _, chain_symbol in ranked:
        for vt_symbol in option_chain_record_symbols(portfolio_name, chain_symbol):
            if vt_symbol in seen:
                continue
            seen.add(vt_symbol)
            symbols.append(vt_symbol)
    return symbols


def ensure_tick_recording_universe(
    portfolios: list[str] | None = None,
    max_chains: int | None = None,
    tick: bool = True,
    bar: bool | None = None,
) -> dict[str, Any]:
    names = portfolios or live_portfolios_from_env()
    chain_limit = record_max_chains_from_env() if max_chains is None else max_chains
    record_bar = env_flag("LIVE_RECORD_BAR", True) if bar is None else bar
    filter_window = apply_recorder_filter_window()
    symbols: list[str] = []
    by_portfolio: dict[str, int] = {}
    for name in names:
        items = portfolio_tick_universe(name, chain_limit)
        by_portfolio[name] = len(items)
        symbols.extend(items)
    result = sync_recorder_universe(symbols, tick=tick, bar=record_bar)
    result["portfolios"] = names
    result["max_chains"] = chain_limit
    result["record_bar"] = record_bar
    result["filter_window"] = filter_window
    result["universe_size"] = len(symbols)
    result["by_portfolio"] = by_portfolio
    kinds = []
    if tick:
        kinds.append("Tick")
    if record_bar:
        kinds.append("K线")
    kind_text = "+".join(kinds) or "行情"
    result["message"] = (
        f"已订阅录制 {kind_text} 合约池：目标 {len(symbols)}，"
        f"新增 {len(result['added'])}，已在列表 {len(result['skipped'])}，"
        f"未找到 {len(result['missing'])}（{record_scope_label(chain_limit)}，"
        f"过滤窗{filter_window}s）"
    )
    return result


def recorder_status() -> dict[str, Any]:
    engine = require_recorder()
    lag = market_data_lag_sec()
    newest = newest_io_if_tick()
    bus: dict[str, Any] = {}
    try:
        from md_bus import md_bus_status

        bus = md_bus_status()
    except Exception:
        bus = {"enabled": False}
    return {
        "tick": sorted(engine.tick_recordings.keys()),
        "bar": sorted(engine.bar_recordings.keys()),
        "interval_sec": engine.timer_interval,
        "active": engine.active,
        "pending": engine.queue.qsize(),
        "record_ticks": should_auto_record_ticks(),
        "record_bar": env_flag("LIVE_RECORD_BAR", True),
        "max_chains": record_max_chains_from_env(),
        "scope": record_scope_label(record_max_chains_from_env()),
        "portfolios": live_portfolios_from_env(),
        "filter_window": int(getattr(engine, "filter_window", record_filter_window_from_env())),
        "md_lag_sec": None if lag is None else round(float(lag), 1),
        "md_max_lag_sec": md_max_lag_from_env(),
        "md_source": (os.getenv("LIVE_MD_SOURCE") or "ctp").strip().lower(),
        "md_bus": bus,
        "newest_tick": None
        if newest is None
        else {
            "vt_symbol": getattr(newest, "vt_symbol", ""),
            "datetime": str(getattr(newest, "datetime", "")),
            "last_price": getattr(newest, "last_price", None),
        },
    }


def add_recordings(vt_symbols: list[str], tick: bool, bar: bool) -> dict[str, Any]:
    engine = require_recorder()
    added: list[str] = []
    skipped: list[str] = []
    missing: list[str] = []
    seen: set[str] = set()
    for vt_symbol in vt_symbols:
        symbol = (vt_symbol or "").strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        if Exchange.LOCAL.value not in symbol and not require_main().get_contract(symbol):
            missing.append(symbol)
            continue
        before_tick = symbol in engine.tick_recordings
        before_bar = symbol in engine.bar_recordings
        if tick:
            engine.add_tick_recording(symbol)
        if bar:
            engine.add_bar_recording(symbol)
        after_tick = symbol in engine.tick_recordings
        after_bar = symbol in engine.bar_recordings
        if (tick and after_tick and not before_tick) or (bar and after_bar and not before_bar):
            added.append(symbol)
        else:
            skipped.append(symbol)
    return {"added": added, "skipped": skipped, "missing": missing, **recorder_status()}


def option_chain_record_symbols(portfolio_name: str, chain_symbol: str) -> list[str]:
    engine = require_option()
    portfolio = engine.portfolios.get(portfolio_name)
    if not portfolio:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到组合 {portfolio_name}")
    chain = get_portfolio_chain(portfolio, chain_symbol)
    if not chain:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到期权链 {chain_symbol}")
    symbols: list[str] = []
    underlying = getattr(chain, "underlying", None)
    if underlying and getattr(underlying, "exchange", None) and getattr(underlying.exchange, "value", "") != "LOCAL":
        symbols.append(underlying.vt_symbol)
    for option in list(chain.calls.values()) + list(chain.puts.values()):
        if option and getattr(option, "vt_symbol", ""):
            symbols.append(option.vt_symbol)
    return symbols


@app.get("/recorder")
def get_recorder(_: bool = Depends(get_access)) -> dict[str, Any]:
    return recorder_status()


@app.get("/md_bus")
def get_md_bus(_: bool = Depends(get_access)) -> dict[str, Any]:
    try:
        from md_bus import md_bus_status

        return md_bus_status()
    except Exception as exc:
        return {"enabled": False, "error": str(exc)}


@app.get("/system/overview")
def get_system_overview(_: bool = Depends(get_access)) -> dict[str, Any]:
    from system_monitor import collect_system_overview

    return collect_system_overview()


@app.get("/system/processes")
def get_system_processes(_: bool = Depends(get_access)) -> dict[str, Any]:
    from system_monitor import collect_process_status

    return collect_process_status()


@app.get("/system/redis")
def get_system_redis(_: bool = Depends(get_access)) -> dict[str, Any]:
    from system_monitor import collect_redis_status

    return collect_redis_status()


@app.get("/system/questdb")
def get_system_questdb(_: bool = Depends(get_access)) -> dict[str, Any]:
    from system_monitor import collect_questdb_status

    return collect_questdb_status()


class RecorderAddModel(BaseModel):
    vt_symbol: str
    tick: bool = True
    bar: bool = True


@app.post("/recorder")
def add_recorder(model: RecorderAddModel, _: bool = Depends(get_access)) -> dict[str, Any]:
    result = add_recordings([model.vt_symbol], model.tick, model.bar)
    if result["missing"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到合约 {model.vt_symbol}")
    kind = []
    if model.tick:
        kind.append("Tick")
    if model.bar:
        kind.append("K线")
    result["message"] = f"已添加 {'+'.join(kind)} 记录 {model.vt_symbol}"
    return result


@app.delete("/recorder")
def remove_recorder(
    vt_symbol: str,
    kind: str = Query("both"),
    _: bool = Depends(get_access),
) -> dict[str, Any]:
    if kind not in {"tick", "bar", "both"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="kind 只能是 tick、bar 或 both")
    engine = require_recorder()
    if kind in {"tick", "both"}:
        engine.remove_tick_recording(vt_symbol)
    if kind in {"bar", "both"}:
        engine.remove_bar_recording(vt_symbol)
    status_data = recorder_status()
    status_data["message"] = f"已移除 {vt_symbol} 的{kind}记录"
    return status_data


class RecorderChainModel(BaseModel):
    portfolio_name: str
    chain_symbol: str = ""
    tick: bool = True
    bar: bool = True


@app.post("/recorder/chain")
def add_recorder_chain(model: RecorderChainModel, _: bool = Depends(get_access)) -> dict[str, Any]:
    engine = require_option()
    portfolio = engine.portfolios.get(model.portfolio_name)
    if not portfolio:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到组合 {model.portfolio_name}")
    chain_symbol = model.chain_symbol or (portfolio_chain_symbols(portfolio)[0] if portfolio_chain_symbols(portfolio) else "")
    if not chain_symbol:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="没有可记录的期权链")
    symbols = option_chain_record_symbols(model.portfolio_name, chain_symbol)
    result = add_recordings(symbols, model.tick, model.bar)
    result["message"] = (
        f"已为 {chain_symbol} 添加记录：新增 {len(result['added'])}，"
        f"已在列表 {len(result['skipped'])}，未找到 {len(result['missing'])}"
    )
    return result


class RecorderUniverseModel(BaseModel):
    portfolios: list[str] = Field(default_factory=list)
    max_chains: int | None = None
    tick: bool = True
    bar: bool = True
    init_portfolio: bool = True


@app.post("/recorder/universe")
def add_recorder_universe(model: RecorderUniverseModel, _: bool = Depends(get_access)) -> dict[str, Any]:
    names = [item.strip() for item in model.portfolios if item.strip()] or live_portfolios_from_env()
    if model.init_portfolio:
        for name in names:
            ok, message = ensure_option_portfolio(name)
            if not ok:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail=f"{name}: {message}。请先连接交易接口并等待合约查询完成",
                )
    return ensure_tick_recording_universe(
        portfolios=names,
        max_chains=model.max_chains,
        tick=model.tick,
        bar=model.bar,
    )


@app.get("/option/portfolio")
def get_option_portfolios(_: bool = Depends(get_access)) -> dict[str, Any]:
    engine = require_option()
    names = set(engine.get_portfolio_names())
    names.update((engine.setting.get("portfolio_settings") or {}).keys())
    items = []
    for name in sorted(names):
        portfolio = engine.portfolios.get(name)
        items.append(
            {
                "name": name,
                "active": name in engine.active_portfolios,
                "setting": engine.get_portfolio_setting(name),
                "net_pos": getattr(portfolio, "net_pos", 0) if portfolio else 0,
                "pos_delta": getattr(portfolio, "pos_delta", 0) if portfolio else 0,
                "pos_gamma": getattr(portfolio, "pos_gamma", 0) if portfolio else 0,
                "pos_vega": getattr(portfolio, "pos_vega", 0) if portfolio else 0,
                "pos_theta": getattr(portfolio, "pos_theta", 0) if portfolio else 0,
                "chains": portfolio_chain_symbols(portfolio) if portfolio else [],
                "option_count": sum(len(chain.options) for chain in (portfolio._chains.values() if portfolio else [])),
            }
        )
    return {"portfolios": items, "models": list(PRICING_MODELS.keys())}


class OptionSettingModel(BaseModel):
    portfolio_name: str
    model_name: str = "Black-76 欧式期货期权"
    interest_rate: float = 0.02
    chain_underlying_map: dict[str, str] = Field(default_factory=dict)
    precision: int = 2


@app.post("/option/portfolio/setting")
def save_option_setting(model: OptionSettingModel, _: bool = Depends(get_access)) -> dict[str, str]:
    engine = require_option()
    if not model.portfolio_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="请先选择期权组合")
    if model.model_name not in PRICING_MODELS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"不支持的定价模型: {model.model_name}")

    missing: list[str] = []
    valid_map: dict[str, str] = {}
    for chain_symbol, underlying_symbol in model.chain_underlying_map.items():
        chain_symbol = chain_symbol.strip()
        underlying_symbol = underlying_symbol.strip()
        if not chain_symbol or not underlying_symbol:
            continue
        if "LOCAL" in underlying_symbol:
            valid_map[chain_symbol] = underlying_symbol
            continue
        if engine.main_engine.get_contract(underlying_symbol):
            valid_map[chain_symbol] = underlying_symbol
        else:
            missing.append(underlying_symbol)

    if model.chain_underlying_map and not valid_map:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="找不到标的合约，请确认已连接交易接口且合约代码有效：" + "、".join(missing),
        )

    apply_option_portfolio_setting(
        engine,
        model.portfolio_name,
        model.model_name,
        model.interest_rate,
        valid_map,
        model.precision,
    )
    portfolio_settings: dict = engine.setting.setdefault("portfolio_settings", {})
    saved = portfolio_settings.setdefault(model.portfolio_name, {})
    saved["chain_underlying_map"] = {
        key.strip(): value.strip()
        for key, value in model.chain_underlying_map.items()
        if key.strip() and value.strip()
    }
    engine.save_setting()

    message = f"已保存组合配置 {model.portfolio_name}"
    if missing:
        message += "；已跳过未找到的标的：" + "、".join(dict.fromkeys(missing))
    return {"message": message}


def ensure_option_portfolio(portfolio_name: str) -> tuple[bool, str]:
    engine = option_engine
    if engine is None:
        return False, "期权引擎未启动"
    live_map = build_live_chain_map(engine, portfolio_name)
    if not live_map:
        return False, "尚未加载到期权合约"
    # Optionally keep only nearest N chains subscribed (MD load control).
    # 0 / negative = all chains. Falls back to LIVE_RECORD_MAX_CHAINS when unset.
    raw_sub = os.getenv("LIVE_SUBSCRIBE_MAX_CHAINS")
    if raw_sub is None or str(raw_sub).strip() == "":
        sub_limit = record_max_chains_from_env()
    else:
        sub_limit = env_int("LIVE_SUBSCRIBE_MAX_CHAINS", 0)
    if sub_limit > 0:
        ranked = sorted(live_map.keys(), key=lambda sym: chain_record_sort_key(sym))
        keep = set(ranked[:sub_limit])
        live_map = {k: v for k, v in live_map.items() if k in keep}
    saved = engine.get_portfolio_setting(portfolio_name)
    apply_option_portfolio_setting(
        engine,
        portfolio_name,
        saved.get("model_name") or "Black-76 欧式期货期权",
        float(saved.get("interest_rate") or 0.02),
        live_map,
        int(saved.get("precision") or 2),
        saved.get("margin_setting") or None,
    )
    try:
        engine.init_portfolio(portfolio_name)
    except AttributeError:
        pass
    portfolio = engine.get_portfolio(portfolio_name)
    # When MD comes from Redis bus, do not subscribe again via CTP MD.
    subscribe_md = not (
        env_flag("LIVE_CTP_SKIP_MD")
        or (os.getenv("LIVE_MD_SOURCE") or "").strip().lower() in {"redis", "bus", "md_bus"}
    )
    for underlying in (portfolio.underlyings or {}).values():
        engine.instruments[underlying.vt_symbol] = underlying
        if subscribe_md and getattr(underlying.exchange, "value", "") != "LOCAL":
            engine.subscribe_data(underlying.vt_symbol)
    for option in (portfolio.options or {}).values():
        if not getattr(option, "underlying", None):
            continue
        engine.instruments[option.vt_symbol] = option
        if subscribe_md:
            engine.subscribe_data(option.vt_symbol)
    try:
        portfolio.calculate_pos_greeks()
    except Exception:
        pass
    suffix = "（行情=Redis）" if not subscribe_md else ""
    return True, f"已初始化组合 {portfolio_name}，期权链 {len(live_map)} 条{suffix}"



def sync_recorder_universe(vt_symbols: list[str], tick: bool, bar: bool) -> dict[str, Any]:
    """Replace recorder lists with the target universe (drop stale symbols)."""
    engine = require_recorder()
    wanted = {s.strip() for s in vt_symbols if s and s.strip()}
    if tick:
        for symbol in list(engine.tick_recordings.keys()):
            if symbol not in wanted:
                engine.remove_tick_recording(symbol)
    if bar:
        for symbol in list(engine.bar_recordings.keys()):
            if symbol not in wanted:
                engine.remove_bar_recording(symbol)
    return add_recordings(sorted(wanted), tick=tick, bar=bar)


class LiveSupervisor:
    """Keep CTP, IO portfolio, tick recorder, and optional iron-condor script alive."""

    def __init__(self) -> None:
        self.portfolios = live_portfolios_from_env()
        self.gateway = os.getenv("LIVE_GATEWAY") or "CTP"
        self.script_name = os.getenv("LIVE_SCRIPT") or "gex_tv_strangle.py"
        self.run_script = env_flag("LIVE_IRON_CONDOR")
        self.record_ticks = should_auto_record_ticks()
        self.record_bar = env_flag("LIVE_RECORD_BAR", True)
        self.max_chains = record_max_chains_from_env()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.loop, name="live-supervisor", daemon=True)
        self.next_connect = 0.0
        self.connect_failures = 0
        self.inited: set[str] = set()
        self.recorded: set[str] = set()
        self.last_log = ""
        self.ctp_ok = False
        self.connecting = False
        self.paused = False
        self.auto_start_script = True
        self.last_md_check = 0.0
        self.last_md_reconnect = 0.0
        self.last_record_refresh = 0.0
        self.td_released = False

    def start(self) -> None:
        os.environ.setdefault("LIVE_PORTFOLIOS", ",".join(self.portfolios))
        os.environ.setdefault("LIVE_DRY_RUN", "0")
        if not self.thread.is_alive():
            self.thread.start()
        parts = [f"组合={','.join(self.portfolios)}"]
        if self.record_ticks:
            kinds = ["Tick"]
            if self.record_bar:
                kinds.append("K线")
            parts.append(
                f"{'+'.join(kinds)}录制{record_scope_label(self.max_chains)}"
                f"/过滤{record_filter_window_from_env()}s"
            )
        if self.run_script or self.auto_start_script:
            parts.append(f"脚本={self.script_name}")
        self.log("实盘守护已启动 " + " ".join(parts))
        try:
            apply_recorder_filter_window()
        except Exception:
            pass

    def pause(self, paused: bool = True) -> None:
        self.paused = bool(paused)
        self.log("实盘守护已暂停" if self.paused else "实盘守护已恢复")

    def apply_setting(self, setting: dict[str, Any]) -> None:
        portfolios = [
            item.strip()
            for item in str(setting.get("portfolios") or "").split(",")
            if item.strip()
        ]
        if portfolios:
            self.portfolios = portfolios
            os.environ["LIVE_PORTFOLIOS"] = ",".join(portfolios)
        if setting.get("gateway"):
            self.gateway = str(setting["gateway"])
            os.environ["LIVE_GATEWAY"] = self.gateway
        if setting.get("script"):
            self.script_name = str(setting["script"])
            os.environ["LIVE_SCRIPT"] = self.script_name
        if "dry_run" in setting:
            os.environ["LIVE_DRY_RUN"] = "1" if bool(setting["dry_run"]) else "0"
        for key, env_name in [
            ("wing_steps", "LIVE_WING_STEPS"),
            ("min_credit_frac", "LIVE_MIN_CREDIT_FRAC"),
            ("min_delta", "LIVE_MIN_DELTA"),
            ("max_delta", "LIVE_MAX_DELTA"),
            ("iv_rank_min", "LIVE_IV_RANK_MIN"),
            ("take_profit", "LIVE_TAKE_PROFIT"),
            ("risk_cap", "LIVE_RISK_CAP"),
            ("max_lots", "LIVE_MAX_LOTS"),
            ("roll_dte", "LIVE_ROLL_DTE"),
        ]:
            if setting.get(key) is None or setting.get(key) == "":
                continue
            os.environ[env_name] = str(setting[key])
        self.auto_start_script = bool(setting.get("auto_start_script", True))
        self.inited.clear()
        self.log(
            f"配置已更新 组合={','.join(self.portfolios)} 脚本={self.script_name} "
            f"dry_run={os.getenv('LIVE_DRY_RUN')} auto={self.auto_start_script}"
        )

    def status(self) -> dict[str, Any]:
        lag = market_data_lag_sec()
        redis_md = env_flag("LIVE_CTP_SKIP_MD") or (
            (os.getenv("LIVE_MD_SOURCE") or "").strip().lower() in {"redis", "bus", "md_bus"}
        )
        return {
            "enabled": True,
            "paused": self.paused,
            "ctp_ok": self.ctp_ok,
            "connecting": self.connecting,
            "portfolios": list(self.portfolios),
            "gateway": self.gateway,
            "script": self.script_name,
            "auto_start_script": self.auto_start_script,
            "session_open": self.cffex_session_open(),
            "md_active": self.cffex_md_active(),
            "redis_md": redis_md,
            "connect_failures": self.connect_failures,
            "inited": sorted(self.inited),
            "next_connect_in": max(0.0, self.next_connect - time.time()),
            "md_lag_sec": None if lag is None else round(float(lag), 1),
            "md_max_lag_sec": md_max_lag_from_env(),
            "record_filter_window": record_filter_window_from_env(),
        }

    def log(self, msg: str) -> None:
        if msg == self.last_log:
            return
        self.last_log = msg
        if main_engine is not None:
            main_engine.write_log(f"[LIVE] {msg}")
        else:
            print(f"[LIVE] {msg}", flush=True)

    def loop(self) -> None:
        self.stop.wait(4.0)
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                self.log("守护异常\n" + traceback.format_exc())
            self.stop.wait(8.0)

    def tick(self) -> None:
        if main_engine is None or option_engine is None:
            return
        if self.run_script and script_engine is None:
            return
        if self.paused:
            return
        if not self.ensure_ctp():
            return
        if not self.ensure_market_data_fresh():
            return
        if self.record_ticks:
            self.ensure_recorder_alive()
        if not self.contracts_ready():
            self.log("等待 IO 期权合约查询完成")
            return
        for name in self.portfolios:
            if name not in self.inited:
                ok, message = ensure_option_portfolio(name)
                if ok:
                    self.log(message)
                    self.inited.add(name)
                else:
                    self.log(message)
                    return
            if self.record_ticks and name not in self.recorded:
                result = ensure_tick_recording_universe(
                    portfolios=[name],
                    max_chains=self.max_chains,
                    tick=True,
                    bar=self.record_bar,
                )
                self.recorded.add(name)
                self.log(result["message"])
        self.maybe_release_td()
        # Periodic re-apply filter window + refresh universe (covers setting wipe / new strikes)
        now = time.time()
        if self.record_ticks and now - self.last_record_refresh > 300:
            self.last_record_refresh = now
            try:
                apply_recorder_filter_window()
                for name in self.portfolios:
                    if name in self.inited:
                        ensure_tick_recording_universe(
                            portfolios=[name],
                            max_chains=self.max_chains,
                            tick=True,
                            bar=self.record_bar,
                        )
            except Exception:
                self.log("周期性刷新录制池失败\n" + traceback.format_exc())
        if self.auto_start_script and self.run_script:
            self.ensure_script()

    def ensure_recorder_alive(self) -> None:
        """Restart DataRecorder writer thread if it died after a DB/write exception."""
        try:
            engine = require_recorder()
        except Exception:
            return
        thread = getattr(engine, "thread", None)
        if engine.active and thread is not None and thread.is_alive():
            return
        self.log("录制线程已停止，正在重启 DataRecorder 写入线程")
        try:
            try:
                while not engine.queue.empty():
                    engine.queue.get_nowait()
            except Exception:
                pass
            engine.active = False
            if thread is not None and thread.is_alive():
                try:
                    thread.join(timeout=1.0)
                except Exception:
                    pass
            engine.thread = threading.Thread(target=engine.run)
            engine.start()
            try:
                apply_recorder_filter_window()
            except Exception:
                pass
            self.log(
                f"录制线程已重启 active={engine.active} pending={engine.queue.qsize()}"
            )
        except Exception:
            self.log("重启录制线程失败\n" + traceback.format_exc())

    def force_gateway_reconnect(self, reason: str) -> None:
        """Close then reconnect gateway — soft connect() alone often leaves MD frozen."""
        assert main_engine is not None
        setting = load_saved_gateway_setting(self.gateway)
        if not setting:
            self.log(f"{reason}，但缺少 {self.gateway} 配置，无法重连")
            self.next_connect = time.time() + 60.0
            return
        self.log(reason)
        gateway = main_engine.gateways.get(self.gateway)
        if gateway is not None:
            try:
                gateway.close()
            except Exception:
                self.log(f"关闭 {self.gateway} 失败\n" + traceback.format_exc())
        main_engine.connect(setting, self.gateway)
        self.connecting = True
        self.ctp_ok = False
        self.inited.clear()
        self.recorded.clear()
        self.connect_failures += 1
        self.next_connect = time.time() + self.connect_backoff_sec()

    def ensure_market_data_fresh(self) -> bool:
        """Reconnect CTP when IF/IO tick timestamps fall far behind wall clock.

        Redis-MD / SKIP_MD live process must NOT thrash the local TD gateway —
        MD is owned by md_receiver. We only gate strategy startup on lag.
        """
        assert main_engine is not None
        if not self.cffex_md_active():
            return True
        now = time.time()
        if now - self.last_md_check < 20:
            return True
        self.last_md_check = now
        lag = market_data_lag_sec()
        if lag is None:
            # Connected but no ticks yet — wait for subscribe/init.
            return True
        max_lag = md_max_lag_from_env()
        if lag <= max_lag:
            return True
        redis_md = env_flag("LIVE_CTP_SKIP_MD") or (
            (os.getenv("LIVE_MD_SOURCE") or "").strip().lower() in {"redis", "bus", "md_bus"}
        )
        if redis_md:
            self.log(
                f"Redis 行情滞后 {int(lag)}s（阈值 {max_lag}s），等待 md_receiver（不重连本机 TD）"
            )
            return False
        # Use a dedicated MD reconnect cooldown (do not inherit login backoff).
        last = float(getattr(self, "last_md_reconnect", 0.0) or 0.0)
        if now - last < 45:
            return False
        self.last_md_reconnect = now
        self.force_gateway_reconnect(
            f"行情时间戳滞后 {int(lag)}s（阈值 {max_lag}s），强制关闭并重连 {self.gateway}"
        )
        return False

    @staticmethod
    def cffex_session_open(now: datetime | None = None) -> bool:
        """Rough CFFEX IO/IF login window (Asia/Shanghai wall clock)."""
        now = now or datetime.now()
        if now.weekday() >= 5:
            return False
        hhmm = now.hour * 100 + now.minute
        # day: 09:00-11:35, 12:55-15:15 (login buffer around official hours)
        return (900 <= hhmm <= 1135) or (1255 <= hhmm <= 1515)

    @staticmethod
    def cffex_md_active(now: datetime | None = None) -> bool:
        """Official continuous auction — only then treat lag as MD failure."""
        now = now or datetime.now()
        if now.weekday() >= 5:
            return False
        hhmm = now.hour * 100 + now.minute
        return (915 <= hhmm <= 1130) or (1300 <= hhmm <= 1500)

    def connect_backoff_sec(self) -> float:
        # Outside session: slow down hard to avoid CTP front thrash.
        if not self.cffex_session_open():
            return min(900.0, 120.0 * max(1, self.connect_failures))
        return min(300.0, 30.0 * (2 ** min(self.connect_failures, 3)))

    def maybe_release_td(self) -> None:
        """Recorder yields CTP TD seat to the live process after MD subscribe is ready."""
        if self.td_released:
            return
        if not env_flag("STANDALONE_RECORDER"):
            return
        if not env_flag("LIVE_RECORDER_RELEASE_TD", True):
            return
        if self.record_ticks and not self.recorded:
            return
        if not self.inited:
            return
        assert main_engine is not None
        gateway = main_engine.gateways.get(self.gateway)
        if gateway is None:
            return
        try:
            from ctp_session import release_ctp_td

            if release_ctp_td(gateway):
                self.td_released = True
                os.environ["LIVE_CTP_SKIP_TD"] = "1"
                self.log("录制进程已释放 CTP 交易通道（保留行情）；实盘进程可登录交易前置")
        except Exception:
            self.log("释放 CTP 交易通道失败\n" + traceback.format_exc())

    def ensure_ctp(self) -> bool:
        assert main_engine is not None
        if not env_flag("LIVE_AUTO_CONNECT_CTP", True):
            # Redis-MD live process may still run strategies once ticks arrive.
            if (os.getenv("LIVE_MD_SOURCE") or "").strip().lower() in {"redis", "bus", "md_bus"}:
                lag = market_data_lag_sec()
                if lag is not None and lag < md_max_lag_from_env() * 2:
                    if not self.ctp_ok:
                        self.ctp_ok = True
                        self.log("行情已由 Redis MD bus 提供（未自动连接 CTP）")
                    return True
            if not getattr(self, "_logged_ctp_disabled", False):
                self.log("已禁用自动连接 CTP（LIVE_AUTO_CONNECT_CTP=0），本进程不登录行情/交易")
                self._logged_ctp_disabled = True
            return False
        # MD-only recorder after TD release: stay connected without TD account.
        if self.td_released or env_flag("LIVE_CTP_SKIP_TD"):
            lag = market_data_lag_sec()
            if lag is not None:
                self.ctp_ok = True
                self.connecting = False
                return True
            if self.contracts_ready():
                self.ctp_ok = True
                return True
        accounts = main_engine.get_all_accounts() or []
        if accounts:
            if not self.ctp_ok:
                self.ctp_ok = True
                self.connect_failures = 0
                self.connecting = False
                self.log("CTP 账户已就绪")
            return True
        # TD-only live process (SKIP_MD): also treat gateway connected via positions/contracts wait.
        if env_flag("LIVE_CTP_SKIP_MD") and not self.connecting:
            # Fall through to connect TD.
            pass
        was_ok = self.ctp_ok
        self.ctp_ok = False
        now = time.time()
        if now < self.next_connect:
            return False
        setting = load_saved_gateway_setting(self.gateway)
        if not setting:
            self.log(f"没有 {self.gateway} 连接配置，无法自动登录")
            self.next_connect = now + 60.0
            return False
        if not self.cffex_session_open():
            self.log(f"非 CFFEX 交易时段，降低 {self.gateway} 重连频率")
        else:
            self.log(f"正在连接 {self.gateway}")
        main_engine.connect(setting, self.gateway)
        self.connecting = True
        if was_ok:
            self.inited.clear()
            self.recorded.clear()
            self.td_released = False
        self.connect_failures += 1
        self.next_connect = now + self.connect_backoff_sec()
        return False

    def contracts_ready(self) -> bool:
        assert main_engine is not None
        needed: set[str] = set()
        for name in self.portfolios:
            needed.add(name.split(".")[0].upper())
        have: set[str] = set()
        for contract in main_engine.get_all_contracts():
            if contract.product != Product.OPTION:
                continue
            symbol = (contract.symbol or "").upper()
            for prefix in list(needed):
                if symbol.startswith(prefix):
                    have.add(prefix)
        return bool(needed) and needed <= have

    def ensure_script(self) -> None:
        assert script_engine is not None
        thread = script_engine.strategy_thread
        if script_engine.strategy_active and thread is not None and not thread.is_alive():
            self.log("策略线程已退出，准备重启")
            script_engine.strategy_active = False
            script_engine.strategy_thread = None
        if script_engine.strategy_active:
            return
        path = resolve_script_path(self.script_name)
        self.log(f"启动脚本 {path.name}")
        script_engine.start_strategy(str(path))


def start_live_supervisor(force: bool = False) -> "LiveSupervisor | None":
    global _live_supervisor
    if not force and not (env_flag("LIVE_IRON_CONDOR") or env_flag("LIVE_RECORD_TICKS")):
        return _live_supervisor
    if _live_supervisor is None:
        _live_supervisor = LiveSupervisor()
        _live_supervisor.start()
        return _live_supervisor
    if not _live_supervisor.thread.is_alive():
        _live_supervisor.thread = threading.Thread(
            target=_live_supervisor.loop, name="live-supervisor", daemon=True
        )
        _live_supervisor.start()
    _live_supervisor.paused = False
    return _live_supervisor


LIVE_SETTING_FILE = "live_trader_setting.json"
LIVE_CONFIG_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "paused": False,
    "auto_start_script": True,
    "portfolios": "IO.CFFEX",
    "gateway": "CTP",
    "script": "gex_tv_strangle.py",
    "dry_run": True,
    "wing_steps": 5,
    "min_credit_frac": 0.30,
    "min_delta": 0.14,
    "max_delta": 0.25,
    "iv_rank_min": 40.0,
    "take_profit": 0.25,
    "risk_cap": 0.06,
    "max_lots": 80,
    "roll_dte": 21,
}


def load_live_setting() -> dict[str, Any]:
    data = load_json(LIVE_SETTING_FILE)
    if not isinstance(data, dict):
        data = {}
    out = dict(LIVE_CONFIG_DEFAULTS)
    out.update({k: v for k, v in data.items() if k in LIVE_CONFIG_DEFAULTS or k in data})
    # Prefer process env / supervisor when already running.
    out["portfolios"] = os.getenv("LIVE_PORTFOLIOS") or out["portfolios"]
    out["gateway"] = os.getenv("LIVE_GATEWAY") or out["gateway"]
    out["script"] = os.getenv("LIVE_SCRIPT") or out["script"]
    if os.getenv("LIVE_DRY_RUN") not in (None, ""):
        out["dry_run"] = env_flag("LIVE_DRY_RUN", bool(out["dry_run"]))
    for key, env_name, cast in [
        ("wing_steps", "LIVE_WING_STEPS", int),
        ("min_credit_frac", "LIVE_MIN_CREDIT_FRAC", float),
        ("min_delta", "LIVE_MIN_DELTA", float),
        ("max_delta", "LIVE_MAX_DELTA", float),
        ("iv_rank_min", "LIVE_IV_RANK_MIN", float),
        ("take_profit", "LIVE_TAKE_PROFIT", float),
        ("risk_cap", "LIVE_RISK_CAP", float),
        ("max_lots", "LIVE_MAX_LOTS", int),
        ("roll_dte", "LIVE_ROLL_DTE", int),
    ]:
        raw = os.getenv(env_name)
        if raw not in (None, ""):
            try:
                out[key] = cast(raw)
            except (TypeError, ValueError):
                pass
    if _live_supervisor is not None:
        out["enabled"] = True
        out["paused"] = bool(_live_supervisor.paused)
        out["auto_start_script"] = bool(_live_supervisor.auto_start_script)
        out["portfolios"] = ",".join(_live_supervisor.portfolios)
        out["gateway"] = _live_supervisor.gateway
        out["script"] = _live_supervisor.script_name
    elif env_flag("LIVE_IRON_CONDOR"):
        out["enabled"] = True
    return out


def save_live_setting(setting: dict[str, Any]) -> dict[str, Any]:
    current = load_live_setting()
    merged = dict(current)
    for key in LIVE_CONFIG_DEFAULTS:
        if key in setting and setting[key] is not None:
            merged[key] = setting[key]
    save_json(LIVE_SETTING_FILE, merged)
    return merged


def collect_live_ticks(symbols: list[str]) -> list[dict[str, Any]]:
    engine = main_engine
    if engine is None:
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for vt_symbol in symbols:
        if not vt_symbol or vt_symbol in seen:
            continue
        seen.add(vt_symbol)
        tick = engine.get_tick(vt_symbol)
        contract = engine.get_contract(vt_symbol)
        if tick is None and contract is None:
            rows.append({"vt_symbol": vt_symbol, "missing": True})
            continue
        rows.append(
            {
                "vt_symbol": vt_symbol,
                "last_price": getattr(tick, "last_price", None) if tick else None,
                "bid_price_1": getattr(tick, "bid_price_1", None) if tick else None,
                "ask_price_1": getattr(tick, "ask_price_1", None) if tick else None,
                "volume": getattr(tick, "volume", None) if tick else None,
                "pricetick": getattr(contract, "pricetick", None) if contract else None,
                "name": getattr(contract, "name", "") if contract else "",
            }
        )
    return rows


def build_live_signals(monitor: dict[str, Any]) -> list[dict[str, Any]]:
    params = monitor.get("params") or {}
    book = monitor.get("book") or {}
    iv_rank_min = float(params.get("iv_rank_min") or 40)
    min_entry = int(params.get("min_entry_dte") or 28)
    max_entry = int(params.get("max_entry_dte") or 65)
    dte = monitor.get("dte")
    lots = float(book.get("lots") or 0)
    signals = [
        {"id": "ctp", "label": "CTP 账户就绪", "ok": bool(monitor.get("ctp_ok")), "detail": ""},
        {
            "id": "session",
            "label": "交易时段",
            "ok": bool(monitor.get("session_ok")),
            "detail": "CFFEX 日盘",
        },
        {
            "id": "iv_rank",
            "label": f"IV Rank ≥ {iv_rank_min:g}",
            "ok": bool(monitor.get("iv_high")),
            "detail": f"当前 {monitor.get('iv_rank', '—')}",
        },
        {
            "id": "range",
            "label": "LSP 区间过滤",
            "ok": bool(monitor.get("range_ok", True)),
            "detail": f"LSP={monitor.get('lsp', '—')}",
        },
        {
            "id": "expand",
            "label": "波动未扩张",
            "ok": bool(monitor.get("expand_ok", True)),
            "detail": "",
        },
        {
            "id": "dte",
            "label": f"DTE 开仓窗 {min_entry}-{max_entry}",
            "ok": dte is not None and min_entry <= int(dte) <= max_entry,
            "detail": f"DTE={dte if dte is not None else '—'}",
        },
        {
            "id": "pick",
            "label": "选出合格铁鹰",
            "ok": bool(monitor.get("pick")),
            "detail": "",
        },
        {
            "id": "flat",
            "label": "当前无持仓可开新仓",
            "ok": lots <= 0,
            "detail": f"lots={lots:g}",
        },
        {
            "id": "script",
            "label": "策略脚本运行中",
            "ok": bool(monitor.get("engine_active") or monitor.get("active")),
            "detail": monitor.get("reason") or "",
        },
    ]
    if monitor.get("pick"):
        pick = monitor["pick"]
        signals.append(
            {
                "id": "structure",
                "label": "候选结构",
                "ok": True,
                "detail": (
                    f"{pick.get('k_put_long')}/{pick.get('k_put')}/"
                    f"{pick.get('k_call')}/{pick.get('k_call_long')} "
                    f"credit={pick.get('credit')}"
                ),
            }
        )
    return signals


def resolve_chain_expiry_info(portfolio_name: str, chain_symbol: str = "") -> dict[str, Any]:
    """Return option expiry plus trading-day and calendar-day DTE."""
    trading = None
    calendar = None
    expiry_text = ""
    symbol = chain_symbol or ""
    if option_engine is not None and portfolio_name:
        portfolio = option_engine.portfolios.get(portfolio_name)
        if portfolio:
            chains = portfolio_chain_symbols(portfolio)
            symbol = symbol or (chains[0] if chains else "")
            chain = get_portfolio_chain(portfolio, symbol) if symbol else None
            if chain:
                trading = int(getattr(chain, "days_to_expiry", 0) or 0) or None
                option = None
                for bucket in (getattr(chain, "calls", None), getattr(chain, "puts", None)):
                    if not bucket:
                        continue
                    option = next(iter(bucket.values()), None)
                    if option:
                        break
                expiry = getattr(option, "option_expiry", None) if option else None
                if expiry is not None:
                    try:
                        if hasattr(expiry, "date"):
                            expiry_date = expiry.date()
                            expiry_text = expiry.strftime("%Y-%m-%d")
                        else:
                            expiry_date = expiry
                            expiry_text = str(expiry)[:10]
                        today = datetime.now().date()
                        calendar = max((expiry_date - today).days, 0)
                    except Exception:
                        pass
                if trading is None and option is not None:
                    try:
                        trading = int(getattr(option, "days_to_expiry", 0) or 0) or None
                    except (TypeError, ValueError):
                        pass
    return {
        "chain_symbol": symbol,
        "expiry": expiry_text,
        "trading_dte": trading,
        "calendar_dte": calendar,
    }


def live_chain_gex_profile(portfolio_name: str, chain_symbol: str = "") -> dict[str, Any]:
    """Build strike-level GEX profile for live indicator explain charts."""
    if option_engine is None or not portfolio_name:
        return {}
    portfolio = option_engine.portfolios.get(portfolio_name)
    if not portfolio:
        return {}
    chains = portfolio_chain_symbols(portfolio)
    symbol = chain_symbol or (chains[0] if chains else "")
    chain = get_portfolio_chain(portfolio, symbol) if symbol else None
    if not chain:
        return {}
    try:
        gex = compute_chain_gex(chain)
    except Exception:
        return {}
    rows = []
    for row in gex.get("strikes") or []:
        rows.append(
            {
                "strike": row.get("strike"),
                "call_gex": row.get("call_gex"),
                "put_gex": row.get("put_gex"),
                "net_gex": row.get("net_gex"),
                "call_oi": row.get("call_oi"),
                "put_oi": row.get("put_oi"),
                "call_gamma": row.get("call_gamma"),
                "put_gamma": row.get("put_gamma"),
            }
        )
    return {
        "source": "live_oi",
        "portfolio": portfolio_name,
        "chain_symbol": symbol or gex.get("spot_chain") or "",
        "spot": gex.get("spot"),
        "underlying": gex.get("underlying"),
        "days_to_expiry": gex.get("days_to_expiry"),
        "call_wall": gex.get("call_wall"),
        "put_wall": gex.get("put_wall"),
        "flip_strike": gex.get("flip_strike"),
        "pin": gex.get("pin"),
        "call_gex": gex.get("call_gex"),
        "put_gex": gex.get("put_gex"),
        "net_gex": gex.get("net_gex"),
        "strikes": rows,
        "formula": "CallGEX = Γ_call × OI_call × S × 1%；PutGEX = −Γ_put × OI_put × S × 1%",
        "rule": "Call墙 = argmax CallGEX；Put墙 = argmin PutGEX（最负）",
    }


def build_live_indicator_explains(data: dict[str, Any]) -> dict[str, Any]:
    """Explain payloads for clickable live metrics (formula + steps + chart data)."""
    indicators = data.get("indicators") or {}
    book = data.get("book") or {}
    pick = data.get("pick") or {}
    kelly = data.get("kelly") or indicators.get("kelly") or {}
    params = data.get("params") or {}
    portfolio = str(data.get("portfolio") or params.get("portfolio_name") or "IO.CFFEX")
    chain = str(data.get("chain") or indicators.get("chain") or "")
    profile = live_chain_gex_profile(portfolio, chain)
    dte_info = resolve_chain_expiry_info(portfolio, chain)
    trading_dte = indicators.get("dte") if indicators.get("dte") is not None else data.get("dte")
    if trading_dte is None:
        trading_dte = dte_info.get("trading_dte")
    calendar_dte = dte_info.get("calendar_dte")
    spot = indicators.get("spot") if indicators.get("spot") is not None else data.get("spot")
    call_wall = indicators.get("call_wall") if indicators.get("call_wall") is not None else data.get("call_wall")
    put_wall = indicators.get("put_wall") if indicators.get("put_wall") is not None else data.get("put_wall")
    profile_call = profile.get("call_wall")
    profile_put = profile.get("put_wall")
    walls_match = (
        profile
        and call_wall is not None
        and put_wall is not None
        and profile_call is not None
        and profile_put is not None
        and abs(float(call_wall) - float(profile_call)) < 1e-6
        and abs(float(put_wall) - float(profile_put)) < 1e-6
    )
    wall_source = "链上真实 OI + theo_gamma" if walls_match else (
        "策略 live_gex_walls / 必要时 synthetic_gex_walls；下图为当前链实时 GEX 剖面供对照"
        if profile.get("strikes")
        else "策略快照（链上剖面暂不可用）"
    )

    def _num(value: Any, digits: int = 4) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "—"
        if digits <= 0:
            return str(int(round(number)))
        return f"{number:.{digits}f}".rstrip("0").rstrip(".")

    explains: dict[str, Any] = {
        "iv": {
            "title": "IV（隐含/代理波动率）",
            "value": indicators.get("iv") if indicators.get("iv") is not None else data.get("iv"),
            "formula": "IV ≈ HV_20 × 1.12（实盘用实现波动代理，待期权 IV 行情可替换）",
            "steps": [
                f"当前值：{_num(indicators.get('iv') if indicators.get('iv') is not None else data.get('iv'), 4)}",
                "取近 20 日收盘实现波动 HV，再乘 1.12 作为开仓定价/墙合成用 σ",
                "IV Rank、铁鹰定价、Delta 带宽均基于该 σ",
            ],
            "chart": {
                "type": "gauge",
                "min": 0,
                "max": 0.6,
                "value": float(indicators.get("iv") or data.get("iv") or 0),
                "label": "IV",
                "zones": [{"to": 0.15, "color": "#54a0ff"}, {"to": 0.30, "color": "#1dd1a1"}, {"to": 0.6, "color": "#ff9f43"}],
            },
        },
        "iv_rank": {
            "title": "IV Rank",
            "value": indicators.get("iv_rank") if indicators.get("iv_rank") is not None else data.get("iv_rank"),
            "formula": "IV Rank = 100 × count(历史HV ≤ 当前IV) / N",
            "steps": [
                f"当前 IV Rank：{_num(indicators.get('iv_rank') if indicators.get('iv_rank') is not None else data.get('iv_rank'), 1)}",
                f"开仓阈值：≥ {params.get('iv_rank_min') or data.get('config', {}).get('iv_rank_min') or 40}",
                f"是否偏高：{'是' if data.get('iv_high') else '否'}",
                "用历史 HV 分布给当前波动率定位百分位，越高越倾向卖波动",
            ],
            "chart": {
                "type": "gauge",
                "min": 0,
                "max": 100,
                "value": float(indicators.get("iv_rank") or data.get("iv_rank") or 0),
                "threshold": float((data.get("config") or {}).get("iv_rank_min") or 40),
                "label": "IV Rank",
            },
        },
        "lsp": {
            "title": "LSP（区间位置）",
            "value": indicators.get("lsp") if indicators.get("lsp") is not None else data.get("lsp"),
            "formula": "LSP = (C − LLV) / (HHV − LLV)，落在 [0,1]",
            "steps": [
                f"当前 LSP：{_num(indicators.get('lsp') if indicators.get('lsp') is not None else data.get('lsp'), 3)}",
                "HHV/LLV 取回看窗口日高低",
                "0 靠近下沿（偏多），1 靠近上沿（偏空）",
                f"开仓窗口：区间过滤 {'通过' if data.get('range_ok') else '未通过'}",
            ],
            "chart": {
                "type": "gauge",
                "min": 0,
                "max": 1,
                "value": float(indicators.get("lsp") or data.get("lsp") or 0.5),
                "label": "LSP",
                "zones": [{"to": 0.35, "color": "#1dd1a1"}, {"to": 0.65, "color": "#54a0ff"}, {"to": 1.0, "color": "#ff6b6b"}],
            },
        },
        "dte": {
            "title": "DTE（剩余到期日）",
            "value": (
                f"交易日 {_num(trading_dte, 0)} ｜ 自然日 {_num(calendar_dte, 0)}"
                if trading_dte is not None or calendar_dte is not None
                else "—"
            ),
            "formula": (
                f"交易日 DTE = 剔除周末与上交所节假日后的剩余交易日（年化基数 {ANNUAL_DAYS}）；"
                "自然日 DTE = 到期日 − 今日"
            ),
            "steps": [
                f"链：{dte_info.get('chain_symbol') or chain or '—'}",
                f"期权到期日：{dte_info.get('expiry') or '—'}",
                f"交易日 DTE：{_num(trading_dte, 0)} 天（vnpy_optionmaster / 策略开仓窗使用）",
                f"自然日 DTE：{_num(calendar_dte, 0)} 天（日历剩余天数）",
                "二者差值为期间周末与节假日；例如国庆长假会拉大差距",
                "开仓窗 / 移仓阈值均按交易日 DTE 判断",
            ],
            "chart": {
                "type": "dual_bar",
                "items": [
                    {
                        "label": "交易日 DTE",
                        "value": float(trading_dte or 0),
                        "color": "#54a0ff",
                    },
                    {
                        "label": "自然日 DTE",
                        "value": float(calendar_dte or 0),
                        "color": "#1dd1a1",
                    },
                ],
                "max": max(90.0, float(trading_dte or 0), float(calendar_dte or 0)) * 1.15 or 90.0,
                "note": f"到期 {dte_info.get('expiry') or '—'}",
            },
        },
        "call_wall": {
            "title": "Call墙",
            "value": call_wall,
            "formula": profile.get("formula") or "CallGEX = Γ × OI × S × 1%；Call墙 = argmax CallGEX",
            "steps": [
                f"策略 Call墙：{_num(call_wall, 0)}",
                f"链上剖面 Call墙：{_num(profile_call, 0) if profile else '—'}",
                f"数据来源：{wall_source}",
                "遍历各行权价算 CallGEX，取正 GEX 最大的 K",
                "铁鹰短 Call 只能开在 max(Call墙, ATM+1档) 外侧",
            ],
            "chart": {
                "type": "gex_walls",
                "highlight": "call",
                "spot": spot or profile.get("spot"),
                "call_wall": call_wall,
                "put_wall": put_wall,
                "profile_call_wall": profile_call,
                "profile_put_wall": profile_put,
                "strikes": profile.get("strikes") or [],
            },
        },
        "put_wall": {
            "title": "Put墙",
            "value": put_wall,
            "formula": profile.get("formula") or "PutGEX = −Γ × OI × S × 1%；Put墙 = argmin PutGEX",
            "steps": [
                f"策略 Put墙：{_num(put_wall, 0)}",
                f"链上剖面 Put墙：{_num(profile_put, 0) if profile else '—'}",
                f"数据来源：{wall_source}",
                "遍历各行权价算 PutGEX，取最负（绝对值最大）的 K",
                "铁鹰短 Put 只能开在 min(Put墙, ATM−1档) 外侧",
            ],
            "chart": {
                "type": "gex_walls",
                "highlight": "put",
                "spot": spot or profile.get("spot"),
                "call_wall": call_wall,
                "put_wall": put_wall,
                "profile_call_wall": profile_call,
                "profile_put_wall": profile_put,
                "strikes": profile.get("strikes") or [],
            },
        },
        "entry_credit": {
            "title": "权利金（净收）",
            "value": indicators.get("entry_credit") if indicators.get("entry_credit") is not None else book.get("entry_credit"),
            "formula": "Credit = (短Call + 短Put) − (长Call + 长Put)",
            "steps": [
                f"账面入场权利金：{_num(book.get('entry_credit'), 2)}",
                f"候选结构权利金：{_num(pick.get('credit') if isinstance(pick, dict) else None, 2)}",
                f"短腿 {book.get('k_put') or '—'}/{book.get('k_call') or '—'}，长腿 {book.get('k_put_long') or '—'}/{book.get('k_call_long') or '—'}",
                "开仓还需满足 净权利金/翼宽 ≥ min_credit_frac",
            ],
            "chart": {
                "type": "legs",
                "legs": [
                    {"name": "短Put", "strike": book.get("k_put") or (pick.get("k_put") if isinstance(pick, dict) else None)},
                    {"name": "短Call", "strike": book.get("k_call") or (pick.get("k_call") if isinstance(pick, dict) else None)},
                    {"name": "长Put", "strike": book.get("k_put_long") or (pick.get("k_put_long") if isinstance(pick, dict) else None)},
                    {"name": "长Call", "strike": book.get("k_call_long") or (pick.get("k_call_long") if isinstance(pick, dict) else None)},
                ],
                "credit": book.get("entry_credit") or (pick.get("credit") if isinstance(pick, dict) else None),
                "spot": spot,
            },
        },
        "kelly": {
            "title": "Kelly f",
            "value": kelly.get("f") if isinstance(kelly, dict) else None,
            "formula": "f_raw = (b·p − (1−p)) / b；f = clip(f_raw × scale, 0, cap)",
            "steps": [
                f"p（区间存活/联合胜率）：{_num(kelly.get('p_leg') if isinstance(kelly, dict) else None, 4)}",
                f"f_raw：{_num(kelly.get('f_raw') if isinstance(kelly, dict) else None, 4)}",
                f"缩放后 f：{_num(kelly.get('f') if isinstance(kelly, dict) else None, 4)}",
                f"预算：{_num(kelly.get('budget') if isinstance(kelly, dict) else None, 0)}（f × NAV）",
                "b 取净权利金/最大亏损；再乘 scale 并受 risk_cap 约束",
            ],
            "chart": {
                "type": "gauge",
                "min": 0,
                "max": 0.2,
                "value": float(kelly.get("f") or 0) if isinstance(kelly, dict) else 0,
                "label": "Kelly f",
            },
        },
        "efficiency": {
            "title": "θ/风险（效率）",
            "value": indicators.get("pick_efficiency") if indicators.get("pick_efficiency") is not None else (pick.get("efficiency") if isinstance(pick, dict) else None),
            "formula": "efficiency ≈ 净Theta / 保证金占用",
            "steps": [
                f"当前效率：{_num(indicators.get('pick_efficiency') if indicators.get('pick_efficiency') is not None else (pick.get('efficiency') if isinstance(pick, dict) else None), 6)}",
                "在墙外、Delta 带宽内的铁鹰候选中，优先选效率更高者",
                f"候选权利金 {_num(pick.get('credit') if isinstance(pick, dict) else None, 2)}，存活概率 {_num(pick.get('range_prob') if isinstance(pick, dict) else None, 4)}",
            ],
            "chart": {
                "type": "bar_single",
                "value": float(
                    indicators.get("pick_efficiency")
                    or (pick.get("efficiency") if isinstance(pick, dict) else 0)
                    or 0
                ),
                "max": max(
                    0.01,
                    float(
                        indicators.get("pick_efficiency")
                        or (pick.get("efficiency") if isinstance(pick, dict) else 0)
                        or 0
                    )
                    * 1.5
                    or 0.01,
                ),
                "label": "θ/风险",
            },
        },
        "range_prob": {
            "title": "存活概率",
            "value": indicators.get("pick_range_prob") if indicators.get("pick_range_prob") is not None else (pick.get("range_prob") if isinstance(pick, dict) else None),
            "formula": "P(短Put < S_T < 短Call)（对数正态到期近似）",
            "steps": [
                f"当前存活概率：{_num(indicators.get('pick_range_prob') if indicators.get('pick_range_prob') is not None else (pick.get('range_prob') if isinstance(pick, dict) else None), 4)}",
                f"候选腿：{pick.get('k_put') if isinstance(pick, dict) else '—'} / {pick.get('k_call') if isinstance(pick, dict) else '—'}",
                "用于 Kelly 的 p_joint，并作为结构优劣参考",
            ],
            "chart": {
                "type": "gauge",
                "min": 0,
                "max": 1,
                "value": float(
                    indicators.get("pick_range_prob")
                    or (pick.get("range_prob") if isinstance(pick, dict) else 0)
                    or 0
                ),
                "label": "存活概率",
            },
        },
        "spot": {
            "title": "标的价格",
            "value": spot,
            "formula": "优先标的中间价 + 调整项；否则用链 ATM",
            "steps": [
                f"当前标的：{_num(spot, 2)}",
                f"组合/链：{portfolio} / {chain or '—'}",
                "用于 ATM、墙、Delta、定价与开平仓判断",
            ],
            "chart": {
                "type": "spot_walls",
                "spot": spot,
                "call_wall": call_wall,
                "put_wall": put_wall,
                "strikes": profile.get("strikes") or [],
            },
        },
    }
    explains["gex_profile"] = profile
    return explains




def live_monitor_payload() -> dict[str, Any]:
    engine = require_script()
    data = (
        safe_load_json("gex_tv_strangle_status.json")
        or safe_load_json("as_option_mm_status.json")
        or {}
    )
    if not isinstance(data, dict):
        data = {}
    data = dict(data)
    data["engine_active"] = bool(engine.strategy_active)
    data["live_supervisor"] = _live_supervisor is not None
    if _live_supervisor is not None:
        data.update({f"supervisor_{k}": v for k, v in _live_supervisor.status().items()})
        data["ctp_ok"] = bool(_live_supervisor.ctp_ok)
        data["live_portfolios"] = list(_live_supervisor.portfolios)
        data["supervisor"] = _live_supervisor.status()
    else:
        data["ctp_ok"] = bool(main_engine.get_all_accounts()) if main_engine else False
        data["supervisor"] = {"enabled": False, "paused": False}
    if not engine.strategy_active:
        data["active"] = False

    book = data.get("book") or {}
    symbols = [
        book.get("call_symbol"),
        book.get("put_symbol"),
        book.get("call_long_symbol"),
        book.get("put_long_symbol"),
    ]
    pick = data.get("pick") or {}
    chain = data.get("chain") or ""
    # Prefer live book legs; otherwise show nothing extra.
    market = collect_live_ticks([s for s in symbols if s])
    accounts = []
    positions = []
    if main_engine is not None:
        accounts = [
            {
                "accountid": getattr(item, "accountid", ""),
                "balance": getattr(item, "balance", 0),
                "available": getattr(item, "available", 0),
                "frozen": getattr(item, "frozen", 0),
            }
            for item in (main_engine.get_all_accounts() or [])
        ]
        positions = [
            {
                "vt_symbol": getattr(item, "vt_symbol", ""),
                "direction": getattr(getattr(item, "direction", None), "value", str(getattr(item, "direction", ""))),
                "volume": getattr(item, "volume", 0),
                "price": getattr(item, "price", 0),
                "pnl": getattr(item, "pnl", 0),
            }
            for item in (main_engine.get_all_positions() or [])
            if float(getattr(item, "volume", 0) or 0) != 0
        ]
    indicators = {
        "spot": data.get("spot"),
        "iv": data.get("iv"),
        "iv_rank": data.get("iv_rank"),
        "lsp": data.get("lsp"),
        "dte": data.get("dte"),
        "nav": data.get("nav"),
        "call_wall": data.get("call_wall"),
        "put_wall": data.get("put_wall"),
        "kelly": data.get("kelly"),
        "entry_credit": book.get("entry_credit"),
        "lots": book.get("lots"),
        "chain": chain,
        "pick_efficiency": pick.get("efficiency") if isinstance(pick, dict) else None,
        "pick_range_prob": pick.get("range_prob") if isinstance(pick, dict) else None,
    }
    data["market"] = market
    data["indicators"] = indicators
    # Prefer structured checklist from API builder; keep strategy raw map under signals_raw.
    if isinstance(data.get("signals"), dict):
        data["signals_raw"] = data.get("signals")
    data["signals"] = build_live_signals(data)
    data["accounts"] = accounts
    data["positions"] = positions
    data["config"] = load_live_setting()
    data["logs"] = list(log_buffer)[-40:]
    data["explains"] = build_live_indicator_explains(data)
    return data


@app.post("/option/portfolio/{portfolio_name}/init")
def init_option_portfolio(portfolio_name: str, _: bool = Depends(get_access)) -> dict[str, Any]:
    ok, message = ensure_option_portfolio(portfolio_name)
    if not ok:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=message + "。请先连接交易接口并等待合约查询完成后再初始化",
        )
    engine = require_option()
    live_map = build_live_chain_map(engine, portfolio_name)
    payload: dict[str, Any] = {"message": message, "chains": list(live_map.keys())}
    if should_auto_record_ticks():
        record = ensure_tick_recording_universe(
            portfolios=[portfolio_name],
            max_chains=record_max_chains_from_env(),
            tick=True,
            bar=env_flag("LIVE_RECORD_BAR", True),
        )
        payload["recorder"] = {
            "added": record.get("added"),
            "skipped": record.get("skipped"),
            "missing": record.get("missing"),
            "universe_size": record.get("universe_size"),
            "message": record.get("message"),
        }
        payload["message"] = f"{message}；{record['message']}"
    return payload


@app.get("/option/underlying/{portfolio_name}")
def get_option_underlyings(portfolio_name: str, _: bool = Depends(get_access)) -> list[str]:
    return require_option().get_underlying_symbols(portfolio_name)


@app.get("/option/chain")
def get_option_chain(
    portfolio_name: str,
    chain_symbol: str = "",
    _: bool = Depends(get_access),
) -> dict[str, Any]:
    engine = require_option()
    portfolio = engine.portfolios.get(portfolio_name)
    if not portfolio:
        return {
            "portfolio": portfolio_name,
            "chain_symbol": chain_symbol,
            "chains": [],
            "rows": [],
            "gex": {},
            "tv_yield": {"spot": 0, "annual_days": ANNUAL_DAYS, "series": []},
            "iv_smile": {"spot": 0, "series": []},
        }
    chains = portfolio_chain_symbols(portfolio)
    if not chain_symbol:
        chain_symbol = chains[0] if chains else ""
    chain = get_portfolio_chain(portfolio, chain_symbol)
    rows = []
    gex: dict[str, Any] = {}
    if chain:
        for index in chain.indexes:
            call = chain.calls.get(index)
            put = chain.puts.get(index)
            rows.append(
                {
                    "index": index,
                    "call": serialize_option(call) if call else None,
                    "put": serialize_option(put) if put else None,
                }
            )
        try:
            gex = compute_chain_gex(chain)
        except Exception:
            gex = {}
    try:
        stack = compute_gex_stack(portfolio, chain_symbol)
    except Exception:
        stack = {}
    if stack.get("months"):
        if not gex:
            gex = {"spot": stack.get("spot"), "strikes": []}
        gex["stack"] = stack
    try:
        tv_yield = compute_tv_yield(portfolio, chain_symbol)
    except Exception:
        tv_yield = {"spot": 0, "annual_days": ANNUAL_DAYS, "series": []}
    try:
        iv_smile = compute_iv_smile(portfolio, chain_symbol)
    except Exception:
        iv_smile = {"spot": 0, "series": []}
    return {
        "portfolio": portfolio_name,
        "chain_symbol": chain_symbol,
        "chains": chains,
        "rows": rows,
        "gex": gex,
        "tv_yield": tv_yield,
        "iv_smile": iv_smile,
    }


class OptionHedgeModel(BaseModel):
    portfolio_name: str
    vt_symbol: str
    timer_trigger: int = 5
    delta_target: int = 0
    delta_range: int = 10
    hedge_payup: int = 0


@app.post("/option/hedge/start")
def start_option_hedge(model: OptionHedgeModel, _: bool = Depends(get_access)) -> dict[str, str]:
    require_option().hedge_engine.start(
        model.portfolio_name,
        model.vt_symbol,
        model.timer_trigger,
        model.delta_target,
        model.delta_range,
        model.hedge_payup,
    )
    return {"message": "Delta 对冲已启动"}


@app.post("/option/hedge/stop")
def stop_option_hedge(_: bool = Depends(get_access)) -> dict[str, str]:
    require_option().hedge_engine.stop()
    return {"message": "Delta 对冲已停止"}


@app.get("/option/hedge")
def get_option_hedge(_: bool = Depends(get_access)) -> dict[str, Any]:
    hedge = require_option().hedge_engine
    return {
        "active": hedge.active,
        "portfolio_name": hedge.portfolio_name,
        "vt_symbol": hedge.vt_symbol,
        "delta_target": hedge.delta_target,
        "delta_range": hedge.delta_range,
    }


@app.get("/spread")
def get_spreads(_: bool = Depends(get_access)) -> list[Any]:
    engine = require_spread()
    items = []
    for name in engine.get_all_spread_names():
        spread = engine.get_spread(name)
        if spread:
            items.append(to_plain(spread.get_item()))
    return items


class SpreadLegModel(BaseModel):
    vt_symbol: str
    variable: str
    trading_direction: int = 1
    trading_multiplier: int = 1


class AddSpreadModel(BaseModel):
    name: str
    price_formula: str
    active_symbol: str
    min_volume: float = 1
    legs: list[SpreadLegModel]


@app.post("/spread")
def add_spread(model: AddSpreadModel, _: bool = Depends(get_access)) -> dict[str, str]:
    require_spread().add_spread(
        model.name,
        [leg.model_dump() for leg in model.legs],
        model.price_formula,
        model.active_symbol,
        model.min_volume,
    )
    return {"message": f"已创建价差 {model.name}"}


@app.delete("/spread/{name}")
def remove_spread(name: str, _: bool = Depends(get_access)) -> dict[str, str]:
    require_spread().remove_spread(name)
    return {"message": f"已移除价差 {name}"}


class StartSpreadAlgoModel(BaseModel):
    spread_name: str
    direction: Direction
    price: float
    volume: float
    payup: int = 0
    interval: int = 5
    lock: bool = False


@app.post("/spread/algo")
def start_spread_algo(model: StartSpreadAlgoModel, _: bool = Depends(get_access)) -> dict[str, str]:
    algoid = require_spread().start_algo(
        model.spread_name,
        model.direction,
        model.price,
        model.volume,
        model.payup,
        model.interval,
        model.lock,
        {},
    )
    if not algoid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="创建价差算法失败")
    return {"algoid": algoid}


@app.delete("/spread/algo/{algoid}")
def stop_spread_algo(algoid: str, _: bool = Depends(get_access)) -> dict[str, str]:
    require_spread().stop_algo(algoid)
    return {"message": f"已停止算法 {algoid}"}


@app.get("/spread/class")
def get_spread_classes(_: bool = Depends(get_access)) -> list[str]:
    return require_spread().get_all_strategy_class_names()


@app.get("/spread/class/{class_name}")
def get_spread_class_parameters(class_name: str, _: bool = Depends(get_access)) -> dict[str, Any]:
    engine = require_spread()
    if class_name not in engine.strategy_engine.classes:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到策略类 {class_name}")
    return to_plain(engine.get_strategy_class_parameters(class_name))


@app.get("/spread/strategy")
def get_spread_strategies(_: bool = Depends(get_access)) -> list[Any]:
    engine = require_spread()
    return [to_plain(strategy.get_data()) for strategy in engine.strategy_engine.strategies.values()]


class AddSpreadStrategyModel(BaseModel):
    class_name: str
    strategy_name: str
    spread_name: str
    setting: dict[str, Any] = Field(default_factory=dict)


@app.post("/spread/strategy")
def add_spread_strategy(model: AddSpreadStrategyModel, _: bool = Depends(get_access)) -> dict[str, str]:
    require_spread().add_strategy(model.class_name, model.strategy_name, model.spread_name, model.setting)
    return {"message": f"已创建价差策略 {model.strategy_name}"}


@app.post("/spread/strategy/{strategy_name}/init")
def init_spread_strategy(strategy_name: str, _: bool = Depends(get_access)) -> dict[str, str]:
    engine = require_spread()
    if strategy_name not in engine.strategy_engine.strategies:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到策略 {strategy_name}")
    engine.init_strategy(strategy_name)
    return {"message": f"正在初始化 {strategy_name}"}


@app.post("/spread/strategy/{strategy_name}/start")
def start_spread_strategy(strategy_name: str, _: bool = Depends(get_access)) -> dict[str, str]:
    engine = require_spread()
    if strategy_name not in engine.strategy_engine.strategies:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到策略 {strategy_name}")
    engine.start_strategy(strategy_name)
    return {"message": f"已启动 {strategy_name}"}


@app.post("/spread/strategy/{strategy_name}/stop")
def stop_spread_strategy(strategy_name: str, _: bool = Depends(get_access)) -> dict[str, str]:
    engine = require_spread()
    if strategy_name not in engine.strategy_engine.strategies:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到策略 {strategy_name}")
    engine.stop_strategy(strategy_name)
    return {"message": f"已停止 {strategy_name}"}


@app.delete("/spread/strategy/{strategy_name}")
def remove_spread_strategy(strategy_name: str, _: bool = Depends(get_access)) -> dict[str, str]:
    engine = require_spread()
    if strategy_name not in engine.strategy_engine.strategies:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"找不到策略 {strategy_name}")
    ok = engine.remove_strategy(strategy_name)
    if not ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="策略移除失败，请先停止")
    return {"message": f"已移除 {strategy_name}"}


@app.get("/script")
def list_scripts(_: bool = Depends(get_access)) -> dict[str, Any]:
    active = False
    try:
        active = bool(require_script().strategy_active)
    except HTTPException:
        pass
    return {
        "active": active,
        "files": list_script_files(),
    }


@app.post("/script/upload")
async def upload_script(file: UploadFile = File(...), _: bool = Depends(get_access)) -> dict[str, str]:
    filename = Path(file.filename or "strategy.py").name
    if not filename.endswith(".py"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="只支持 .py 脚本")
    target = script_dir().joinpath(filename)
    target.write_bytes(await file.read())
    return {"path": str(target), "message": f"已保存 {filename}"}


class StartScriptModel(BaseModel):
    path: str


@app.post("/script/start")
def start_script(model: StartScriptModel, _: bool = Depends(get_access)) -> dict[str, str]:
    path = resolve_script_path(model.path)
    require_script().start_strategy(str(path))
    return {"message": f"已启动脚本 {path.name}"}


@app.post("/script/stop")
def stop_script(_: bool = Depends(get_access)) -> dict[str, str]:
    require_script().stop_strategy()
    return {"message": "已停止脚本"}


@app.get("/script/monitor")
def get_script_monitor(_: bool = Depends(get_access)) -> dict[str, Any]:
    engine = require_script()
    data = (
        safe_load_json("gex_tv_strangle_status.json")
        or safe_load_json("io_covered_call_status.json")
        or safe_load_json("as_option_mm_status.json")
        or {}
    )
    data["engine_active"] = bool(engine.strategy_active)
    data["live_supervisor"] = _live_supervisor is not None
    if _live_supervisor is not None:
        data["live_portfolios"] = list(_live_supervisor.portfolios)
        data["ctp_ok"] = bool(_live_supervisor.ctp_ok)
    if not engine.strategy_active:
        data["active"] = False
    return data


class ScriptBacktestModel(BaseModel):
    engine: str = "gex"
    kind: str = "SA"
    interval: str = "1d"
    name: str = "自定义参数"
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
    roll_dte: int = 21
    risk_cap: float = 0.06
    max_lots: int = 80
    iv_rank_min: float = 40.0
    take_profit: float = 0.25
    compare: bool = False
    seed: int = 42


class ScriptCacheModel(BaseModel):
    engine: str = "gex"
    kind: str = "SA"
    interval: str = "1d"


@app.get("/script/backtest")
def get_script_backtest(
    engine: str = Query("gex"),
    kind: str = Query("SA"),
    interval: str = Query("1d"),
    _: bool = Depends(get_access),
) -> dict[str, Any]:
    return script_backtest_snapshot(engine, kind, interval)


@app.post("/script/backtest")
def start_script_backtest(model: ScriptBacktestModel, _: bool = Depends(get_access)) -> dict[str, str]:
    with script_bt_lock:
        if script_bt_state["running"]:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="已有脚本回测或缓存任务在运行")
        script_bt_state["running"] = True
        script_bt_state["phase"] = "backtest"
        script_bt_state["message"] = "已开始脚本回测"
        script_bt_state["error"] = ""
        script_bt_state["engine"] = _norm_script_engine(model.engine)
        script_bt_state["kind"] = _norm_script_kind(model.kind)
        script_bt_state["interval"] = _norm_script_interval(model.interval)
    threading.Thread(
        target=run_script_backtest_job,
        args=(model.model_dump(),),
        daemon=True,
        name="script-backtest",
    ).start()
    return {"message": "已开始脚本回测"}


@app.post("/script/backtest/cache")
def refresh_script_backtest_cache(
    model: ScriptCacheModel = Body(default_factory=ScriptCacheModel),
    _: bool = Depends(get_access),
) -> dict[str, str]:
    payload = model.model_dump()
    with script_bt_lock:
        if script_bt_state["running"]:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="已有脚本回测或缓存任务在运行")
        script_bt_state["running"] = True
        script_bt_state["phase"] = "cache"
        script_bt_state["message"] = "已开始刷新行情缓存"
        script_bt_state["error"] = ""
        script_bt_state["engine"] = _norm_script_engine(model.engine)
        script_bt_state["kind"] = _norm_script_kind(model.kind)
        script_bt_state["interval"] = _norm_script_interval(model.interval)
    threading.Thread(
        target=run_script_cache_job,
        args=(payload,),
        daemon=True,
        name="script-backtest-cache",
    ).start()
    return {"message": "已开始刷新行情缓存"}


class ScriptOptimizeModel(BaseModel):
    name: str = "寻优"
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
    seed: int = 42
    objective: str = "sharpe"
    hedge_mode: str = "on"
    gamma_start: float = 0.04
    gamma_end: float = 0.16
    gamma_step: float = 0.04
    kappa_start: float = 0.8
    kappa_end: float = 2.2
    kappa_step: float = 0.7
    spread_start: float = 0.003
    spread_end: float = 0.021
    spread_step: float = 0.009
    tau_start: float = 0.1
    tau_end: float = 0.25
    tau_step: float = 0.075


@app.post("/script/backtest/optimize")
def start_script_optimize(model: ScriptOptimizeModel, _: bool = Depends(get_access)) -> dict[str, str]:
    with script_bt_lock:
        if script_bt_state["running"]:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="已有脚本回测或缓存任务在运行")
        script_bt_state["running"] = True
        script_bt_state["phase"] = "optimize"
        script_bt_state["message"] = "已开始参数寻优"
        script_bt_state["error"] = ""
        script_bt_state["progress"] = {"done": 0, "total": 0}
    threading.Thread(
        target=run_script_optimize_job,
        args=(model.model_dump(),),
        daemon=True,
        name="script-optimize",
    ).start()
    return {"message": "已开始参数寻优"}


class LiveConfigModel(BaseModel):
    enabled: bool | None = None
    paused: bool | None = None
    auto_start_script: bool | None = None
    portfolios: str | None = None
    gateway: str | None = None
    script: str | None = None
    dry_run: bool | None = None
    wing_steps: int | None = None
    min_credit_frac: float | None = None
    min_delta: float | None = None
    max_delta: float | None = None
    iv_rank_min: float | None = None
    take_profit: float | None = None
    risk_cap: float | None = None
    max_lots: int | None = None
    roll_dte: int | None = None


@app.get("/live/config")
def get_live_config(_: bool = Depends(get_access)) -> dict[str, Any]:
    return load_live_setting()


@app.put("/live/config")
def put_live_config(model: LiveConfigModel, _: bool = Depends(get_access)) -> dict[str, Any]:
    payload = {k: v for k, v in model.model_dump().items() if v is not None}
    merged = save_live_setting(payload)
    if merged.get("enabled"):
        supervisor = start_live_supervisor(force=True)
        if supervisor is not None:
            supervisor.apply_setting(merged)
            if merged.get("paused"):
                supervisor.pause(True)
            else:
                supervisor.pause(False)
    elif _live_supervisor is not None:
        _live_supervisor.apply_setting(merged)
        _live_supervisor.pause(True)
    return {"message": "实盘配置已保存", "config": load_live_setting()}


@app.get("/live/status")
def get_live_status(_: bool = Depends(get_access)) -> dict[str, Any]:
    script = require_script()
    setting = load_live_setting()
    supervisor = _live_supervisor.status() if _live_supervisor is not None else {
        "enabled": False,
        "paused": False,
        "ctp_ok": bool(main_engine.get_all_accounts()) if main_engine else False,
        "session_open": LiveSupervisor.cffex_session_open(),
    }
    return {
        "config": setting,
        "supervisor": supervisor,
        "script_active": bool(script.strategy_active),
        "script_files": [path.name for path in Path("/app/scripts").glob("*.py")]
        if Path("/app/scripts").exists()
        else [path.name for path in Path(__file__).resolve().parent.joinpath("scripts").glob("*.py")],
        "gateway_connected": bool(main_engine.get_all_accounts()) if main_engine else False,
        "account_count": len(main_engine.get_all_accounts() or []) if main_engine else 0,
        "position_count": len(
            [p for p in (main_engine.get_all_positions() or []) if float(getattr(p, "volume", 0) or 0)]
        )
        if main_engine
        else 0,
        "updated": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }


@app.get("/live/monitor")
def get_live_monitor(_: bool = Depends(get_access)) -> dict[str, Any]:
    return live_monitor_payload()


@app.post("/live/start")
def live_start(_: bool = Depends(get_access)) -> dict[str, Any]:
    setting = load_live_setting()
    setting["enabled"] = True
    setting["paused"] = False
    setting["dry_run"] = bool(setting.get("dry_run", False))
    save_live_setting(setting)
    os.environ["LIVE_IRON_CONDOR"] = "1"
    supervisor = start_live_supervisor(force=True)
    assert supervisor is not None
    supervisor.run_script = True
    supervisor.apply_setting(setting)
    supervisor.pause(False)
    supervisor.auto_start_script = True
    # Kick CTP / portfolio / script promptly.
    try:
        supervisor.tick()
    except Exception:
        traceback.print_exc()
    script = require_script()
    if not script.strategy_active:
        path = resolve_script_path(supervisor.script_name)
        script.start_strategy(str(path))
    return {
        "message": f"已启动实盘：{supervisor.script_name}",
        "supervisor": supervisor.status(),
        "script_active": bool(script.strategy_active),
        "config": load_live_setting(),
    }


@app.post("/live/stop")
def live_stop(_: bool = Depends(get_access)) -> dict[str, str]:
    script = require_script()
    if script.strategy_active:
        script.stop_strategy()
    if _live_supervisor is not None:
        _live_supervisor.auto_start_script = False
        _live_supervisor.pause(True)
    setting = load_live_setting()
    setting["paused"] = True
    setting["auto_start_script"] = False
    save_live_setting(setting)
    # Mark status inactive for UI.
    for name in ("gex_tv_strangle_status.json", "as_option_mm_status.json"):
        data = load_json(name)
        if isinstance(data, dict):
            data["active"] = False
            data["updated"] = datetime.now().isoformat(sep=" ", timespec="seconds")
            save_json(name, data)
    return {"message": "已停止策略脚本，并暂停自动拉起"}


@app.post("/live/pause")
def live_pause(_: bool = Depends(get_access)) -> dict[str, str]:
    if _live_supervisor is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="实盘守护未启用")
    _live_supervisor.pause(True)
    setting = load_live_setting()
    setting["paused"] = True
    save_live_setting(setting)
    return {"message": "实盘守护已暂停（不自动重连/拉起）"}


@app.post("/live/resume")
def live_resume(_: bool = Depends(get_access)) -> dict[str, str]:
    supervisor = start_live_supervisor(force=True)
    if supervisor is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="无法启用实盘守护")
    supervisor.pause(False)
    setting = load_live_setting()
    setting["enabled"] = True
    setting["paused"] = False
    save_live_setting(setting)
    return {"message": "实盘守护已恢复"}


@app.websocket("/ws/")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str | None = Query(None),
) -> None:
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    try:
        decode_username(token)
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    active_websockets.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in active_websockets:
            active_websockets.remove(websocket)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
