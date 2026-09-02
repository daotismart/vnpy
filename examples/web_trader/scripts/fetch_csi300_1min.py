"""Download CSI 300 1-minute bars via AkShare (Sina) and cache them locally."""

from __future__ import annotations

import json
from pathlib import Path

CACHE = Path(__file__).with_name("csi300_1min_cache.json")


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


def main() -> None:
    disable_system_proxy()
    import akshare as ak

    df = ak.stock_zh_a_minute(symbol="sh000300", period="1", adjust="")
    if df is None or df.empty:
        raise RuntimeError("AkShare 未返回沪深300 1分钟数据")

    rows = []
    for rec in df.itertuples(index=False):
        stamp = str(getattr(rec, "day"))
        rows.append(
            [
                stamp,
                float(rec.open),
                float(rec.high),
                float(rec.low),
                float(rec.close),
                float(getattr(rec, "volume") or 0),
            ]
        )
    rows.sort(key=lambda row: row[0])
    CACHE.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    days = sorted({row[0][:10] for row in rows})
    print(
        json.dumps(
            {
                "bars": len(rows),
                "days": len(days),
                "start": rows[0][0],
                "end": rows[-1][0],
                "source": "akshare.stock_zh_a_minute sh000300 period=1",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
