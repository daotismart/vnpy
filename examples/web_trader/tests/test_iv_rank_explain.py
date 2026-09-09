"""IV Rank explain popup should expose HV series and calculation steps."""

from __future__ import annotations

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
    assert "iv_rank_calc" in script
    assert '"hv_hist": [round(float(x), 4) for x in self.hv_hist]' in script
    assert "drawExplainIvRankHist" in app_js
    assert 'chart.type === "iv_rank_hist"' in app_js
    assert "live-explain-table" in index
    assert "renderLiveExplainTable" in app_js


def test_iv_rank_formula_math() -> None:
    history = [0.10, 0.12, 0.15, 0.18, 0.20, 0.22]
    current = 0.16
    below = sum(1 for item in history if item <= current)
    rank = 100.0 * below / len(history)
    assert below == 3
    assert abs(rank - 50.0) < 1e-9
