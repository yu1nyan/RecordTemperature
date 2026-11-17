"""Absolute humidity and the influenza guide (same thresholds as the dashboard and the Windows widget)."""
from __future__ import annotations

FLU_WARNING_BELOW = 7.0     # g/m³: 警戒
FLU_CAUTION_BELOW = 11.0    # g/m³: 注意


def absolute_humidity(temp_c: float, rh: float) -> float:
    """Absolute humidity [g/m³] from temperature [℃] and relative humidity [%] (Tetens)."""
    es = 6.1078 * 10 ** (7.5 * temp_c / (temp_c + 237.3))
    return 216.7 * (rh / 100 * es) / (temp_c + 273.15)


def flu_level(ah: float) -> str:
    """'warning' (< 7 g/m³), 'caution' (< 11 g/m³) or 'low'."""
    if ah < FLU_WARNING_BELOW:
        return "warning"
    if ah < FLU_CAUTION_BELOW:
        return "caution"
    return "low"


FLU_LABEL = {"warning": "警戒", "caution": "注意", "low": "低リスク"}
