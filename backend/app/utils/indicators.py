"""
Technical indicators module for market analysis.

Provides EMA, SMA, RSI, MACD, and Bollinger Bands calculations
for use in AI assessments, backtesting, and trading strategies.

Ported/inspired by 0xrsydn/polymarket-crypto-toolkit indicators package.
"""
import math
from typing import List, Optional, Dict, Any, Tuple


def sma(prices: List[float], period: int) -> List[Optional[float]]:
    """Simple Moving Average.

    Returns a list of the same length as *prices* where the first
    ``period - 1`` entries are ``None`` (insufficient data).
    """
    if period < 1:
        raise ValueError("SMA period must be >= 1")
    result: List[Optional[float]] = [None] * len(prices)
    if len(prices) < period:
        return result
    window_sum = sum(prices[:period])
    result[period - 1] = window_sum / period
    for i in range(period, len(prices)):
        window_sum += prices[i] - prices[i - period]
        result[i] = window_sum / period
    return result


def ema(prices: List[float], period: int) -> List[Optional[float]]:
    """Exponential Moving Average.

    Uses the standard multiplier ``2 / (period + 1)`` and seeds the first
    EMA value with the SMA over the initial *period* prices.
    """
    if period < 1:
        raise ValueError("EMA period must be >= 1")
    result: List[Optional[float]] = [None] * len(prices)
    if len(prices) < period:
        return result
    k = 2.0 / (period + 1)
    # Seed with SMA
    seed = sum(prices[:period]) / period
    result[period - 1] = seed
    prev = seed
    for i in range(period, len(prices)):
        val = prices[i] * k + prev * (1 - k)
        result[i] = val
        prev = val
    return result


def rsi(prices: List[float], period: int = 14) -> List[Optional[float]]:
    """Relative Strength Index (Wilder smoothing).

    Returns values in ``[0, 100]``.  The first ``period`` entries are
    ``None`` (need ``period + 1`` prices to compute the first value).
    """
    if period < 1:
        raise ValueError("RSI period must be >= 1")
    result: List[Optional[float]] = [None] * len(prices)
    if len(prices) < period + 1:
        return result

    # Calculate initial gains/losses
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, period + 1):
        delta = prices[i] - prices[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100.0 - (100.0 / (1.0 + rs))

    # Wilder smoothing for subsequent values
    for i in range(period + 1, len(prices)):
        delta = prices[i] - prices[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            result[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i] = 100.0 - (100.0 / (1.0 + rs))

    return result


def macd(
    prices: List[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> Dict[str, List[Optional[float]]]:
    """MACD (Moving Average Convergence Divergence).

    Returns a dict with keys ``"macd"``, ``"signal"``, ``"histogram"``.
    """
    fast_ema = ema(prices, fast_period)
    slow_ema = ema(prices, slow_period)

    n = len(prices)
    macd_line: List[Optional[float]] = [None] * n
    for i in range(n):
        if fast_ema[i] is not None and slow_ema[i] is not None:
            macd_line[i] = fast_ema[i] - slow_ema[i]

    # Signal line = EMA of the MACD line
    macd_values = [v for v in macd_line if v is not None]
    signal_ema = ema(macd_values, signal_period) if len(macd_values) >= signal_period else [None] * len(macd_values)

    signal_line: List[Optional[float]] = [None] * n
    histogram: List[Optional[float]] = [None] * n

    # Map signal EMA back to original indices
    macd_idx = 0
    for i in range(n):
        if macd_line[i] is not None:
            if macd_idx < len(signal_ema):
                signal_line[i] = signal_ema[macd_idx]
            if macd_line[i] is not None and signal_line[i] is not None:
                histogram[i] = macd_line[i] - signal_line[i]
            macd_idx += 1

    return {"macd": macd_line, "signal": signal_line, "histogram": histogram}


def bollinger_bands(
    prices: List[float],
    period: int = 20,
    num_std: float = 2.0,
) -> Dict[str, List[Optional[float]]]:
    """Bollinger Bands.

    Returns ``{"upper": [...], "middle": [...], "lower": [...]}``.
    """
    middle = sma(prices, period)
    upper: List[Optional[float]] = [None] * len(prices)
    lower: List[Optional[float]] = [None] * len(prices)

    for i in range(period - 1, len(prices)):
        window = prices[i - period + 1 : i + 1]
        mean = middle[i]
        if mean is None:
            continue
        variance = sum((p - mean) ** 2 for p in window) / period
        std = math.sqrt(variance)
        upper[i] = mean + num_std * std
        lower[i] = mean - num_std * std

    return {"upper": upper, "middle": middle, "lower": lower}


def compute_all_indicators(
    prices: List[float],
    rsi_period: int = 14,
    sma_period: int = 20,
    ema_period: int = 12,
    bb_period: int = 20,
    bb_std: float = 2.0,
) -> Dict[str, Any]:
    """Compute all indicators at once and return a summary dict.

    Returns the latest value for each indicator plus the full series.
    Useful for feeding into LLM prompts or backtesting engines.
    """
    def _last_value(series: List[Optional[float]]) -> Optional[float]:
        for v in reversed(series):
            if v is not None:
                return round(v, 6)
        return None

    sma_series = sma(prices, sma_period)
    ema_series = ema(prices, ema_period)
    rsi_series = rsi(prices, rsi_period)
    macd_data = macd(prices)
    bb_data = bollinger_bands(prices, bb_period, bb_std)

    return {
        "price_count": len(prices),
        "latest_price": prices[-1] if prices else None,
        "sma": {
            "period": sma_period,
            "latest": _last_value(sma_series),
            "series": sma_series,
        },
        "ema": {
            "period": ema_period,
            "latest": _last_value(ema_series),
            "series": ema_series,
        },
        "rsi": {
            "period": rsi_period,
            "latest": _last_value(rsi_series),
            "series": rsi_series,
        },
        "macd": {
            "latest_macd": _last_value(macd_data["macd"]),
            "latest_signal": _last_value(macd_data["signal"]),
            "latest_histogram": _last_value(macd_data["histogram"]),
            "series": macd_data,
        },
        "bollinger_bands": {
            "period": bb_period,
            "num_std": bb_std,
            "latest_upper": _last_value(bb_data["upper"]),
            "latest_middle": _last_value(bb_data["middle"]),
            "latest_lower": _last_value(bb_data["lower"]),
            "series": bb_data,
        },
    }


def generate_indicator_summary(indicators: Dict[str, Any]) -> str:
    """Generate a human-readable summary for LLM prompts."""
    lines: List[str] = []
    price = indicators.get("latest_price")
    lines.append(f"Current Price: {price}")

    sma_data = indicators.get("sma", {})
    if sma_data.get("latest") is not None:
        trend = "above" if price and price > sma_data["latest"] else "below"
        lines.append(f"SMA({sma_data['period']}): {sma_data['latest']} — price is {trend}")

    ema_data = indicators.get("ema", {})
    if ema_data.get("latest") is not None:
        trend = "above" if price and price > ema_data["latest"] else "below"
        lines.append(f"EMA({ema_data['period']}): {ema_data['latest']} — price is {trend}")

    rsi_data = indicators.get("rsi", {})
    if rsi_data.get("latest") is not None:
        rsi_val = rsi_data["latest"]
        zone = "overbought" if rsi_val > 70 else "oversold" if rsi_val < 30 else "neutral"
        lines.append(f"RSI({rsi_data['period']}): {rsi_val:.1f} ({zone})")

    macd_data = indicators.get("macd", {})
    if macd_data.get("latest_macd") is not None:
        signal = "bullish" if (macd_data.get("latest_histogram") or 0) > 0 else "bearish"
        lines.append(
            f"MACD: {macd_data['latest_macd']:.4f}, "
            f"Signal: {macd_data['latest_signal']}, "
            f"Histogram: {macd_data['latest_histogram']:.4f} ({signal})"
        )

    bb_data = indicators.get("bollinger_bands", {})
    if bb_data.get("latest_upper") is not None and price:
        bb_width = bb_data["latest_upper"] - bb_data["latest_lower"]
        position = "near upper" if price > bb_data["latest_middle"] else "near lower"
        lines.append(
            f"BBands({bb_data['period']},{bb_data['num_std']}): "
            f"[{bb_data['latest_lower']:.4f}, {bb_data['latest_middle']:.4f}, "
            f"{bb_data['latest_upper']:.4f}] width={bb_width:.4f} ({position})"
        )

    return "\n".join(lines)
