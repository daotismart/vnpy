"""Download SA (CZCE soda ash) 5-minute bars: Eastmoney 主连 + Sina 分月合约补洞."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

CACHE = Path(__file__).with_name("sa_5min_cache.json")


def disable_system_proxy() -> None:
    from requests.sessions import Session

    if getattr(Session.__init__, "_vnpy_no_proxy", False):
        return
    original = Session.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.trust_env = False

    init._vnpy_no_proxy = True  # type: ignore[attr-defined]
    Session.__init__ = init  # type: ignore[method-assign]


def _stamp(value: object) -> str:
    text = str(value).replace("/", "-").replace("T", " ")
    if len(text) >= 16:
        return text[:19] if len(text) >= 19 else text[:16] + ":00"
    return text[:10] + " 00:00:00"


def frame_to_rows(df) -> list[list]:
    cols = {str(name).strip().lower(): name for name in df.columns}

    def col(*names: str):
        for name in names:
            if name in df.columns:
                return name
            key = name.strip().lower()
            if key in cols:
                return cols[key]
        return None

    date_col = col("datetime", "时间", "date", "日期")
    open_col = col("open", "开盘", "开盘价")
    high_col = col("high", "最高", "最高价")
    low_col = col("low", "最低", "最低价")
    close_col = col("close", "收盘", "收盘价")
    vol_col = col("volume", "成交量", "vol")
    if not all([date_col, open_col, high_col, low_col, close_col]):
        raise RuntimeError(f"无法识别列: {list(df.columns)}")
    rows = []
    for rec in df.itertuples(index=False):
        mapping = rec._asdict()
        close = float(mapping[close_col] or 0)
        if close <= 0:
            continue
        volume = float(mapping[vol_col] or 0) if vol_col else 0.0
        rows.append(
            [
                _stamp(mapping[date_col]),
                float(mapping[open_col]),
                float(mapping[high_col]),
                float(mapping[low_col]),
                close,
                volume,
            ]
        )
    return rows


def merge_rows(bag: dict[str, list], rows: list[list]) -> None:
    for row in rows:
        old = bag.get(row[0])
        if old is None or float(row[5]) >= float(old[5]):
            bag[row[0]] = row


def fetch_eastmoney_page(sec_id: str, end: str, limit: int = 2000) -> list[list]:
    import requests

    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": sec_id,
        "klt": "5",
        "fqt": "1",
        "lmt": str(limit),
        "end": end,
        "iscca": "1",
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "forcect": "1",
    }
    last_exc: Exception | None = None
    for _ in range(4):
        try:
            r = requests.get(url, timeout=20, params=params)
            payload = r.json() or {}
            klines = ((payload.get("data") or {}).get("klines")) or []
            rows = []
            for item in klines:
                parts = str(item).split(",")
                if len(parts) < 5:
                    continue
                close = float(parts[2] or 0)
                if close <= 0:
                    continue
                rows.append(
                    [
                        _stamp(parts[0]),
                        float(parts[1]),
                        float(parts[3]),
                        float(parts[4]),
                        close,
                        float(parts[5] or 0) if len(parts) > 5 else 0.0,
                    ]
                )
            return rows
        except Exception as exc:
            last_exc = exc
            time.sleep(1.2)
    if last_exc:
        raise last_exc
    return []


def fetch_eastmoney_main() -> list[list]:
    from akshare.futures.futures_hist_em import __get_exchange_symbol_map

    c_contract_mkt, c_contract_to_e_contract, _, _ = __get_exchange_symbol_map()
    name = "纯碱主连"
    if name not in c_contract_mkt:
        return []
    sec_id = f"{c_contract_mkt[name]}.{c_contract_to_e_contract[name]}"
    bag: dict[str, list] = {}
    end = "20500000"
    for _ in range(36):
        page = fetch_eastmoney_page(sec_id, end, 2000)
        if not page:
            break
        before = len(bag)
        merge_rows(bag, page)
        if len(bag) == before:
            break
        first = min(bag)
        day = datetime.strptime(first[:10], "%Y-%m-%d") - timedelta(days=1)
        end = day.strftime("%Y%m%d")
        if day.year < 2019:
            break
        time.sleep(0.25)
    return [bag[key] for key in sorted(bag)]


def sa_month_codes() -> list[str]:
    codes = ["SA0"]
    year, month = 2019, 12
    while (year, month) <= (2026, 9):
        codes.append(f"SA{year % 100:02d}{month:02d}")
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return codes


def fetch_sina_months() -> list[list]:
    import akshare as ak

    bag: dict[str, list] = {}
    for symbol in sa_month_codes():
        try:
            df = ak.futures_zh_minute_sina(symbol=symbol, period="5")
            if df is not None and not df.empty:
                merge_rows(bag, frame_to_rows(df))
        except Exception:
            pass
        time.sleep(0.2)
    return [bag[key] for key in sorted(bag)]


def main() -> None:
    disable_system_proxy()
    bag: dict[str, list] = {}
    sources: list[str] = []
    try:
        em = fetch_eastmoney_main()
        merge_rows(bag, em)
        sources.append(f"eastmoney 纯碱主连 5min x{len(em)}")
        print(f"eastmoney {len(em)}")
    except Exception as exc:
        sources.append(f"eastmoney skip {type(exc).__name__}")
        print(f"eastmoney skip {type(exc).__name__}")
    sina = fetch_sina_months()
    merge_rows(bag, sina)
    sources.append(f"sina SA months 5min x{len(sina)}")
    print(f"sina {len(sina)}")
    rows = [bag[key] for key in sorted(bag)]
    if len(rows) < 80:
        raise RuntimeError("未拿到足够的 SA 5 分钟线")
    CACHE.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    days = sorted({row[0][:10] for row in rows})
    print(
        json.dumps(
            {
                "bars": len(rows),
                "days": len(days),
                "start": rows[0][0],
                "end": rows[-1][0],
                "source": " + ".join(sources),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
