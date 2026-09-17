"""IV Rank explain popup should expose dated HV series and calculation steps."""

from __future__ import annotations

import importlib.util
import json
import math
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"
SCRIPT = ROOT / "scripts" / "gex_tv_strangle.py"
APP_JS = ROOT / "static" / "app.js"
INDEX = ROOT / "static" / "index.html"


def test_sources_expose_iv_rank_series() -> None:
    server = SERVER.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    app_js = APP_JS.read_text(encoding="utf-8")
    index = INDEX.read_text(encoding="utf-8")

    assert "def resolve_iv_rank_series" in server
    assert '"type": "iv_rank_hist"' in server
    assert "def _rebuild_hv_series_from_bars" in server
    assert '"date": row.get("date")' in server or '"date": row.get("date") or ""' in server
    assert "day_dates" in script
    assert "iv_rank_calc" in script
    assert '"hv_hist": [round(float(x), 4) for x in self.hv_hist]' in script
    assert "drawExplainIvRankHist" in app_js
    assert "formatBarDate" in app_js
    assert 'chart.type === "iv_rank_hist"' in app_js
    assert "live-explain-table" in index
    assert "renderLiveExplainTable" in app_js
    assert "20260916ivdate" in index


def test_iv_rank_formula_math() -> None:
    history = [0.10, 0.12, 0.15, 0.18, 0.20, 0.22]
    current = 0.16
    below = sum(1 for item in history if item <= current)
    rank = 100.0 * below / len(history)
    assert below == 3
    assert abs(rank - 50.0) < 1e-9


def _load_iv_helpers():
    source = SERVER.read_text(encoding="utf-8")
    start = source.index("def _realized_hv_from_closes")
    end = source.index("\ndef build_live_indicator_explains")
    chunk = source[start:end]
    ns: dict = {
        "Any": object,
        "datetime": __import__("datetime").datetime,
        "timedelta": __import__("datetime").timedelta,
        "json": json,
        "math": math,
        "importlib": __import__("importlib"),
        "Path": Path,
        "WEB_SCRIPTS_DIR": ROOT / "scripts",
    }
    exec(chunk, ns)
    return ns


def test_hv_series_uses_bar_end_dates(tmp_path: Path | None = None) -> None:
    h = _load_iv_helpers()
    bars = []
    px = 4000.0
    # 30 synthetic trading days
    for i in range(30):
        day = f"2026-08-{i + 1:02d}" if i < 28 else f"2026-09-{i - 27:02d}"
        px *= 1.001 if i % 2 == 0 else 0.999
        bars.append({"date": day, "close": px})
    series = h["_rebuild_hv_series_from_bars"](bars, hv_lookback=5, iv_rank_lookback=20)
    assert series
    assert all(row.get("date") for row in series)
    assert series[-1]["date"] == bars[-1]["date"]
    # Full hist has 25 points; lookback keeps last 20 → first kept end-date is bars[10]
    assert series[0]["date"] == bars[10]["date"]
    assert series[0]["date"] != "1"
    assert "-" in series[0]["date"]


def test_resolve_iv_rank_series_prefers_dates(tmp_path: Path | None = None) -> None:
    h = _load_iv_helpers()
    bars = []
    px = 4500.0
    for i in range(40):
        month = 7 if i < 20 else 8
        day = (i % 20) + 1
        stamp = f"2026-{month:02d}-{day:02d}"
        px *= 1.002 if i % 3 else 0.998
        bars.append({"date": stamp, "close": round(px, 2)})
    # Patch loader to return our bars without network refresh.
    h["_load_daily_bars_for_portfolio"] = lambda *a, **k: (bars, "daily_cache")
    out = h["resolve_iv_rank_series"](
        {
            "portfolio": "IO.CFFEX",
            "iv": 0.2,
            "hv": 0.18,
            "params": {"hv_lookback": 5, "iv_rank_lookback": 20},
            "iv_rank_calc": {"hv_lookback": 5, "iv_rank_lookback": 20, "iv_factor": 1.12},
        }
    )
    assert out["series"]
    assert out["start_date"]
    assert out["end_date"]
    assert out["series"][0]["date"]
    assert out["series"][-1]["date"] == out["end_date"]
    assert all("date" in row for row in out["series"])


if __name__ == "__main__":
    test_sources_expose_iv_rank_series()
    test_iv_rank_formula_math()
    test_hv_series_uses_bar_end_dates()
    test_resolve_iv_rank_series_prefers_dates()
    print("ok: iv rank explain dates")
