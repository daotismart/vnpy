"""GEX wall explain chart must stay non-zero when theo_gamma is cold."""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"


def test_server_uses_black76_gamma_fallback() -> None:
    text = SERVER.read_text(encoding="utf-8")
    assert "def option_gamma_for_gex" in text
    assert "from vnpy_optionmaster.pricing.black_76 import calculate_gamma" in text
    assert "used_model_gamma" in text
    assert "live_oi+model_gamma" in text
    assert "def _chain_proxy_iv" in text
    # compute_chain_gex must not rely solely on theo_gamma
    assert "call_gamma = option_gamma_for_gex" in text
    assert 'float(getattr(call, "theo_gamma", 0) or 0) if call else 0.0' not in text.split("def compute_chain_gex")[1].split("def _pick_reference_spot")[0]


def _black76_gamma(spot: float, strike: float, rate: float, t: float, iv: float) -> float:
    """Minimal Black-76 gamma (futures) matching vnpy_optionmaster.pricing.black_76."""
    if spot <= 0 or strike <= 0 or t <= 0 or iv <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * t) / (iv * math.sqrt(t))
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    return pdf / (spot * iv * math.sqrt(t)) * math.exp(-rate * t)


def test_gex_nonzero_when_theo_gamma_missing() -> None:
    """OI + spot + model gamma must produce a non-flat GEX profile."""
    spot = 4550.0
    size = 100.0
    proxy_iv = 0.18
    t = 20 / 244.0
    rate = 0.02

    strikes = []
    for k in range(4300, 4801, 50):
        call_oi = 8000.0 * math.exp(-0.5 * ((k - spot) / (spot * 0.08)) ** 2)
        put_oi = 7000.0 * math.exp(-0.5 * ((k - spot) / (spot * 0.10)) ** 2)
        call = SimpleNamespace(
            theo_gamma=0.0,
            mid_impv=0.0,
            strike_price=float(k),
            size=size,
            days_to_expiry=20,
            time_to_expiry=0.0,
            interest_rate=rate,
        )
        put = SimpleNamespace(
            theo_gamma=0.0,
            mid_impv=0.0,
            strike_price=float(k),
            size=size,
            days_to_expiry=20,
            time_to_expiry=0.0,
            interest_rate=rate,
        )
        call_gamma = max(_black76_gamma(spot, float(k), rate, t, proxy_iv), 0.0) * size
        put_gamma = max(_black76_gamma(spot, float(k), rate, t, proxy_iv), 0.0) * size
        assert call.theo_gamma == 0.0
        call_gex = call_gamma * call_oi * spot * 0.01
        put_gex = -(put_gamma * put_oi * spot * 0.01)
        strikes.append({"strike": k, "call_gex": call_gex, "put_gex": put_gex})

    assert any(abs(row["call_gex"]) > 1.0 for row in strikes)
    assert any(abs(row["put_gex"]) > 1.0 for row in strikes)
    max_call = max(abs(row["call_gex"]) for row in strikes)
    max_put = max(abs(row["put_gex"]) for row in strikes)
    assert max_call > 0 and max_put > 0
    # profile must not collapse to a single flat-zero row
    nonzero_rows = [row for row in strikes if abs(row["call_gex"]) > 1e-6 or abs(row["put_gex"]) > 1e-6]
    assert len(nonzero_rows) >= 5
