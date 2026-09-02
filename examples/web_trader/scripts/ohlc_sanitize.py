"""Shared OHLC sanitizers for public futures caches (Sina / Eastmoney)."""

from __future__ import annotations

from typing import Iterable

# Sina minute feeds sometimes emit Q20 fixed-point integers (~price * 2**20).
Q20 = float(1 << 20)
SA_MIN_PRICE = 200.0
SA_MAX_PRICE = 5000.0
DEFAULT_MAX_JUMP = 0.18


def _maybe_unscale(value: float) -> float:
    price = float(value)
    if price > 1_000_000.0:
        return price / Q20
    return price


def normalize_ohlc(open_: float, high: float, low: float, close: float) -> tuple[float, float, float, float] | None:
    """Unscale Q20 garbage and require a plausible OHLC envelope."""
    o = _maybe_unscale(open_)
    h = _maybe_unscale(high)
    l = _maybe_unscale(low)
    c = _maybe_unscale(close)
    if min(o, h, l, c) <= 0:
        return None
    # If any leg still looks like raw Q20, reject.
    if max(o, h, l, c) > 100_000:
        return None
    hi = max(o, h, l, c)
    lo = min(o, h, l, c)
    return o, hi, lo, c


def sanitize_bar(
    row: list,
    *,
    min_price: float = SA_MIN_PRICE,
    max_price: float = SA_MAX_PRICE,
) -> list | None:
    """Return a cleaned [dt, o, h, l, c, v] row, or None if unusable."""
    if len(row) < 5:
        return None
    normalized = normalize_ohlc(float(row[1]), float(row[2]), float(row[3]), float(row[4]))
    if normalized is None:
        return None
    o, h, l, c = normalized
    if c < min_price or c > max_price or h < min_price or l > max_price:
        return None
    if h < l or h < c or l > c:
        return None
    volume = float(row[5] or 0) if len(row) > 5 else 0.0
    return [str(row[0]), o, h, l, c, volume]


def merge_sane_rows(
    bag: dict[str, list],
    rows: Iterable[list],
    *,
    min_price: float = SA_MIN_PRICE,
    max_price: float = SA_MAX_PRICE,
) -> int:
    """Merge sanitized bars; prefer higher volume on timestamp collisions."""
    kept = 0
    for raw in rows:
        row = sanitize_bar(raw, min_price=min_price, max_price=max_price)
        if row is None:
            continue
        old = bag.get(row[0])
        if old is None or float(row[5]) >= float(old[5]):
            bag[row[0]] = row
        kept += 1
    return kept


def filter_price_jumps(
    rows: list[list],
    *,
    max_jump: float = DEFAULT_MAX_JUMP,
) -> list[list]:
    """Drop bars that gap too hard vs the last accepted close (cross-contract noise)."""
    out: list[list] = []
    prev_close: float | None = None
    for row in rows:
        close = float(row[4])
        if prev_close is not None and prev_close > 0:
            jump = abs(close / prev_close - 1.0)
            if jump > max_jump:
                continue
        out.append(row)
        prev_close = close
    return out
