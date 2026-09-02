"""Build IF 30-minute bars by resampling the IF 5-minute cache."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fetch_sa_30min import snap_rows  # noqa: E402

CACHE = ROOT.joinpath("if_30min_cache.json")
CACHE_5MIN = ROOT.joinpath("if_5min_cache.json")


def main() -> None:
    if not CACHE_5MIN.exists() or CACHE_5MIN.stat().st_size < 100:
        raise RuntimeError("缺少 IF 5 分钟缓存，请先运行 fetch_if_5min.py")
    raw = json.loads(CACHE_5MIN.read_text(encoding="utf-8"))
    rows = snap_rows(raw)
    if len(rows) < 80:
        raise RuntimeError("IF 30 分钟重采样样本不足")
    CACHE.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    days = sorted({row[0][:10] for row in rows})
    print(
        json.dumps(
            {
                "bars": len(rows),
                "days": len(days),
                "start": rows[0][0],
                "end": rows[-1][0],
                "source": f"resample if_5min x{len(raw)} → 30min x{len(rows)}",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
