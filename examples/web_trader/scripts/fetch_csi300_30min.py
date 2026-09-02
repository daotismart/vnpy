"""Download CSI 300 30-minute bars via AkShare and cache them locally."""

from __future__ import annotations

import json
from pathlib import Path

CACHE = Path(__file__).with_name("csi300_30min_cache.json")


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


def rows_from_frame(df) -> list[list]:
    time_col = "时间" if "时间" in df.columns else "day"
    open_col = "开盘" if "开盘" in df.columns else "open"
    high_col = "最高" if "最高" in df.columns else "high"
    low_col = "最低" if "最低" in df.columns else "low"
    close_col = "收盘" if "收盘" in df.columns else "close"
    vol_col = "成交量" if "成交量" in df.columns else "volume"
    rows = []
    for rec in df.itertuples(index=False):
        mapping = rec._asdict()
        stamp = str(mapping[time_col])
        volume = mapping.get(vol_col, 0) or 0
        rows.append(
            [
                stamp,
                float(mapping[open_col]),
                float(mapping[high_col]),
                float(mapping[low_col]),
                float(mapping[close_col]),
                float(volume),
            ]
        )
    rows.sort(key=lambda row: row[0])
    return rows


def fetch_eastmoney_30min():
    import akshare.index.index_zh_em as module

    module.index_code_id_map_em = lambda: {"000300": "1"}
    return module.index_zh_a_hist_min_em(
        symbol="000300",
        period="30",
        start_date="2025-08-19 09:30:00",
        end_date="2026-08-19 15:00:00",
    )


def fetch_sina_30min():
    import akshare as ak

    return ak.stock_zh_a_minute(symbol="sh000300", period="30", adjust="")


def main() -> None:
    disable_system_proxy()
    source = ""
    df = None
    try:
        df = fetch_eastmoney_30min()
        source = "akshare.index_zh_a_hist_min_em 000300 period=30"
    except Exception as exc:
        print(f"eastmoney skip: {type(exc).__name__} {exc}")

    if df is None or df.empty:
        df = fetch_sina_30min()
        source = "akshare.stock_zh_a_minute sh000300 period=30"

    if df is None or df.empty:
        raise RuntimeError("AkShare 未返回沪深300 30分钟数据")

    rows = rows_from_frame(df)
    CACHE.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    days = sorted({row[0][:10] for row in rows})
    print(
        json.dumps(
            {
                "bars": len(rows),
                "days": len(days),
                "start": rows[0][0],
                "end": rows[-1][0],
                "source": source,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
