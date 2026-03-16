"""Market context — regime detection, day classification, and sentiment analysis."""
from __future__ import annotations

# ── tbot_core.context.regime ────────────────────────────────────
from tbot_core.context.regime import (
    RegimeContext,
    compute_regime_context,
    compute_scalp_regime_context,
    log_regime_context,
    classify_atr_regime,
    classify_adx_tier,
)

# ── tbot_core.context.day_type ──────────────────────────────────
from tbot_core.context.day_type import (
    DayTypeClassifier,
    DayTypeResult,
    DayType,
    PivotContext,
    build_pivot_context,
    make_day_type_classifier,
    apply_day_type_to_threshold,
    apply_day_type_to_pm,
)

# ── tbot_core.context.sentiment ─────────────────────────────────
from tbot_core.context.sentiment import (
    get_daily_sentiment,
    get_daily_sentiment_from_candles,
    compute_intraday_sentiment,
    classify_day_type,
    get_opening_bias,
    get_open_position_bias,
)

__all__ = [
    # regime
    "RegimeContext", "compute_regime_context", "compute_scalp_regime_context",
    "log_regime_context", "classify_atr_regime", "classify_adx_tier",
    # day_type
    "DayTypeClassifier", "DayTypeResult", "DayType", "PivotContext",
    "build_pivot_context", "make_day_type_classifier",
    "apply_day_type_to_threshold", "apply_day_type_to_pm",
    # sentiment
    "get_daily_sentiment", "get_daily_sentiment_from_candles",
    "compute_intraday_sentiment", "classify_day_type",
    "get_opening_bias", "get_open_position_bias",
]
