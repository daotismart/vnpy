"""Smoke tests for local tick/bar series viewers (data menu)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"
DB = ROOT / "vnpy_questdb" / "questdb_database.py"
APP_JS = ROOT / "static" / "app.js"
INDEX = ROOT / "static" / "index.html"


def choose_tick_sample_interval(start: datetime, end: datetime, max_points: int = 2000) -> str:
    """Mirror of QuestdbDatabase.choose_tick_sample_interval for offline checks."""
    span = max(1.0, (end - start).total_seconds())
    target = max(100, int(max_points))
    seconds_per_point = span / target
    table = [
        (1.0, "1s"),
        (5.0, "5s"),
        (15.0, "15s"),
        (30.0, "30s"),
        (60.0, "1m"),
        (300.0, "5m"),
        (900.0, "15m"),
        (1800.0, "30m"),
        (3600.0, "1h"),
    ]
    chosen = "1h"
    for threshold, label in table:
        if seconds_per_point <= threshold:
            chosen = label
            break
    return chosen


def main() -> int:
    server = SERVER.read_text(encoding="utf-8")
    db = DB.read_text(encoding="utf-8")
    html = INDEX.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")

    assert '@app.get("/data/tick/series")' in server
    assert '@app.get("/data/bar/series")' in server
    assert "def query_tick_series" in server
    assert "def query_bar_series" in server
    assert "def _aggregate_bars" in server
    assert "def load_tick_series" in db
    assert "SAMPLE BY" in db
    assert 'id="tick-view-modal"' in html
    assert 'id="bar-view-modal"' in html
    assert 'id="bar-view-chart"' in html
    assert 'data-tick="view"' in js
    assert 'data-data="view"' in js
    assert "function openTickViewModal" in js
    assert "function openBarViewModal" in js
    assert "function drawBarViewChart" in js
    assert "/data/tick/series" in js
    assert "/data/bar/series" in js
    assert 'data-tick="export"' not in js
    assert 'data-data="export"' not in js

    start = datetime(2026, 9, 7, 9, 30, 0)
    assert choose_tick_sample_interval(start, start + timedelta(minutes=10)) == "1s"
    assert choose_tick_sample_interval(start, start + timedelta(hours=6)) in {"15s", "30s", "1m"}
    assert choose_tick_sample_interval(start, start + timedelta(days=5)) in {"5m", "15m", "30m", "1h"}
    print("test_tick_series_view: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
