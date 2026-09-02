"""Download IF (CFFEX CSI 300 index futures) dominant-contract daily bars."""

from __future__ import annotations

import json
from pathlib import Path

CACHE = Path(__file__).with_name("if_daily_cache.json")


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


def _col(df, *names):
    columns = list(df.columns)
    lowered = {str(name).strip().lower(): name for name in columns}
    for name in names:
        if name in columns:
            return name
        key = str(name).strip().lower()
        if key in lowered:
            return lowered[key]
    return None


def frame_to_rows(df) -> list[list]:
    date_col = _col(df, "date", "datetime", "时间", "日期")
    open_col = _col(df, "open", "开盘", "开盘价", "open_price")
    high_col = _col(df, "high", "最高", "最高价")
    low_col = _col(df, "low", "最低", "最低价")
    close_col = _col(df, "close", "收盘", "收盘价", "close_price")
    vol_col = _col(df, "volume", "成交量", "vol", "hold")
    if not all([date_col, open_col, high_col, low_col, close_col]):
        raise RuntimeError(f"无法识别列: {list(df.columns)}")
    rows = []
    for rec in df.itertuples(index=False):
        mapping = rec._asdict()
        stamp = str(mapping[date_col])[:10].replace("/", "-")
        if len(stamp) == 8 and stamp.isdigit():
            stamp = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"
        close = float(mapping[close_col] or 0)
        if close <= 0:
            continue
        volume = float(mapping[vol_col] or 0) if vol_col else 0.0
        rows.append(
            [
                stamp,
                float(mapping[open_col]),
                float(mapping[high_col]),
                float(mapping[low_col]),
                close,
                volume,
            ]
        )
    rows.sort(key=lambda row: row[0])
    dedup: dict[str, list] = {}
    for row in rows:
        dedup[row[0]] = row
    return [dedup[key] for key in sorted(dedup)]


def fetch() -> tuple[object, str]:
    import akshare as ak

    errors: list[str] = []
    attempts = [
        ("futures_main_sina IF0", lambda: ak.futures_main_sina(symbol="IF0")),
        ("futures_zh_daily_sina IF0", lambda: ak.futures_zh_daily_sina(symbol="IF0")),
        ("index_zh_a_hist 000300", lambda: ak.index_zh_a_hist(symbol="000300", period="daily", start_date="20191223", end_date="20260831")),
    ]
    for label, func in attempts:
        try:
            df = func()
            if df is not None and not df.empty:
                return df, label
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__} {exc}")
    raise RuntimeError("AkShare 未返回 IF/沪深300 日线: " + " | ".join(errors))


def main() -> None:
    disable_system_proxy()
    df, source = fetch()
    rows = frame_to_rows(df)
    if len(rows) < 80:
        raise RuntimeError(f"IF 日线样本不足: {len(rows)} 来源 {source}")
    CACHE.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    print(
        json.dumps(
            {
                "bars": len(rows),
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
