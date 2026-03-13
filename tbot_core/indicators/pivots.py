"""Pivot indicators: CPR, Traditional Pivots, Camarilla Pivots, CPR Width."""


def calculate_cpr(prev_high, prev_low, prev_close):
    """Calculate Central Pivot Range (CPR).

    Returns dict with 'pivot', 'bc' (bottom central), 'tc' (top central).
    """
    pivot = (prev_high + prev_low + prev_close) / 3
    bc = (pivot + prev_low) / 2
    tc = (pivot + prev_high) / 2

    if round(tc, 2) == round(bc, 2):
        tc = pivot + 0.0005 * pivot
        bc = pivot - 0.0005 * pivot

    return {"pivot": round(pivot, 2), "bc": round(bc, 2), "tc": round(tc, 2)}


def calculate_traditional_pivots(prev_high, prev_low, prev_close):
    """Calculate Traditional (Floor) Pivots: P, R1-R5, S1-S5."""
    pivot = (prev_high + prev_low + prev_close) / 3
    r1 = (2 * pivot) - prev_low
    s1 = (2 * pivot) - prev_high
    r2 = pivot + (prev_high - prev_low)
    s2 = pivot - (prev_high - prev_low)
    r3 = pivot + 2 * (prev_high - prev_low)
    s3 = pivot - 2 * (prev_high - prev_low)
    r4 = pivot + 3 * (prev_high - prev_low)
    s4 = pivot - 3 * (prev_high - prev_low)
    r5 = pivot + 4 * (prev_high - prev_low)
    s5 = pivot - 4 * (prev_high - prev_low)

    if prev_high == prev_low:
        r1 = pivot + 0.0005 * pivot
        s1 = pivot - 0.0005 * pivot
        r2 = pivot + 0.001 * pivot
        s2 = pivot - 0.001 * pivot
        r3 = pivot + 0.002 * pivot
        s3 = pivot - 0.002 * pivot
        r4 = pivot + 0.003 * pivot
        s4 = pivot - 0.003 * pivot
        r5 = pivot + 0.004 * pivot
        s5 = pivot - 0.004 * pivot

    return {
        "pivot": round(pivot, 2),
        "r1": round(r1, 2), "s1": round(s1, 2),
        "r2": round(r2, 2), "s2": round(s2, 2),
        "r3": round(r3, 2), "s3": round(s3, 2),
        "r4": round(r4, 2), "s4": round(s4, 2),
        "r5": round(r5, 2), "s5": round(s5, 2),
    }


def calculate_camarilla_pivots(prev_high, prev_low, prev_close):
    """Calculate Camarilla Pivots: R1-R6, S1-S6."""
    range_val = prev_high - prev_low
    if range_val == 0:
        range_val = 0.001 * prev_close

    r1 = prev_close + (range_val * 1.1 / 12)
    r2 = prev_close + (range_val * 1.1 / 6)
    r3 = prev_close + (range_val * 1.1 / 4)
    r4 = prev_close + (range_val * 1.1 / 2)
    s1 = prev_close - (range_val * 1.1 / 12)
    s2 = prev_close - (range_val * 1.1 / 6)
    s3 = prev_close - (range_val * 1.1 / 4)
    s4 = prev_close - (range_val * 1.1 / 2)
    r5 = (prev_high / max(prev_low, 0.0001)) * prev_close
    s5 = prev_close - (r5 - prev_close)
    r6 = prev_close + (range_val * 1.1 * 2)
    s6 = prev_close - (range_val * 1.1 * 2)

    return {
        "r1": round(r1, 2), "r2": round(r2, 2),
        "r3": round(r3, 2), "r4": round(r4, 2),
        "r5": round(r5, 2), "r6": round(r6, 2),
        "s1": round(s1, 2), "s2": round(s2, 2),
        "s3": round(s3, 2), "s4": round(s4, 2),
        "s5": round(s5, 2), "s6": round(s6, 2),
    }


def classify_cpr_width(cpr_levels, close_price=None):
    """Classify CPR width as day-type context signal.

    NARROW (<0.30% of price) -> trending breakout day
    NORMAL (0.30-0.80%)      -> regular day
    WIDE   (>0.80%)          -> range-bound day

    Returns "NARROW" | "NORMAL" | "WIDE".
    """
    try:
        tc = float(cpr_levels.get("tc", 0))
        bc = float(cpr_levels.get("bc", 0))
        width = abs(tc - bc)
    except Exception:
        return "NORMAL"

    if close_price and close_price > 0:
        width_pct = (width / close_price) * 100
        if width_pct < 0.30:
            return "NARROW"
        if width_pct < 0.80:
            return "NORMAL"
        return "WIDE"

    # Absolute fallback (NIFTY typical: 25000-26000)
    if width < 50:
        return "NARROW"
    if width < 150:
        return "NORMAL"
    return "WIDE"
