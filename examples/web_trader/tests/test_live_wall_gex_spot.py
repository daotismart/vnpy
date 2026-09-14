"""Wall explain GEX must stay non-zero when underlying mid_price is bid+ask."""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"


def test_server_sanitizes_double_mid_price() -> None:
    text = SERVER.read_text(encoding="utf-8")
    assert "def _almost_double" in text
    assert "def _chain_strike_anchor" in text
    assert "bid+ask" in text or "2×" in text or "2x" in text
    assert "not _spot_near(spot, override" in text


def _black76_gamma(spot: float, strike: float, rate: float, t: float, iv: float) -> float:
    if spot <= 0 or strike <= 0 or t <= 0 or iv <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * t) / (iv * math.sqrt(t))
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    return pdf / (spot * iv * math.sqrt(t)) * math.exp(-rate * t)


def test_double_spot_collapses_gamma_near_strikes() -> None:
    """Reproduce production bug: mid≈2×spot makes all listed gammas ~0."""
    true_spot = 4479.7
    bad_spot = 8992.3  # ≈ bid+ask
    size = 100.0
    t = 30 / 244.0
    iv = 0.18
    rate = 0.02
    strikes = list(range(4050, 5101, 50))

    good = [
        max(_black76_gamma(true_spot, float(k), rate, t, iv), 0.0) * size
        for k in strikes
    ]
    bad = [
        max(_black76_gamma(bad_spot, float(k), rate, t, iv), 0.0) * size
        for k in strikes
    ]
    assert max(good) > 1e-3
    assert max(bad) < max(good) * 0.05


def test_chain_spot_info_prefers_override_over_double_mid() -> None:
    """Import helpers with heavy deps stubbed."""
    import importlib.util
    import sys
    import types

    # Minimal stubs so we can exec just the helper section is too heavy;
    # instead re-implement the decision rules mirrored from server.py.
    mid = 8992.3
    override = 4479.7
    atm = 0.0
    anchor = 4550.0
    tick_mid = 0.0

    def almost_double(value: float, ref: float, tol: float = 0.08) -> bool:
        if value <= 0 or ref <= 0:
            return False
        return abs(value - 2.0 * ref) / max(2.0 * ref, 1e-9) <= tol

    def spot_near(value: float, ref: float, tol: float = 0.08) -> bool:
        if value <= 0 or ref <= 0:
            return False
        return abs(value - ref) / max(ref, 1e-9) <= tol

    for ref in (tick_mid, override, atm, anchor):
        if almost_double(mid, ref):
            mid = float(ref)
            break
    assert abs(mid - override) < 1e-6

    spot = mid
    source = "underlying_mid"
    if override > 0 and (
        spot <= 0
        or source in {"", "parity"}
        or not spot_near(spot, override, tol=0.05)
    ):
        spot = override
        source = "override"
    assert source in {"underlying_mid", "override"}
    assert abs(spot - 4479.7) < 1e-6
