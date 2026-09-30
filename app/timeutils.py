"""UTC 时间解析与半开 [start, end) 时间窗工具。"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import List, Tuple


class TimeError(ValueError):
    """时间字符串非法。"""


def parse_utc(value: object, *, field: str = "时刻") -> datetime:
    """解析 ISO 8601 时间，必须带时区信息（内部统一转 UTC，朴素时间拒绝）。"""
    if not isinstance(value, str) or not value.strip():
        raise TimeError(f"{field}必须是非空 ISO 8601 字符串，收到: {value!r}")
    text = value.strip()
    # 兼容结尾 Z
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TimeError(f"{field}不是合法的 ISO 8601 时间: {value!r}") from exc
    if dt.tzinfo is None:
        raise TimeError(f"{field}必须携带时区偏移（如 Z 或 +00:00）: {value!r}")
    return dt.astimezone(timezone.utc)


def validate_window(start: datetime, end: datetime) -> None:
    if not (math.isfinite(start.timestamp()) and math.isfinite(end.timestamp())):
        raise TimeError("生效时刻必须为有限值")
    if start >= end:
        raise TimeError(f"生效时段必须左闭右开且长度为正: [{start.isoformat()}, {end.isoformat()})")


def merge_windows(windows: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """合并重叠或端点相接的 UTC epoch 秒区间。"""
    ordered = sorted(windows)
    merged: List[Tuple[float, float]] = []
    for a, b in ordered:
        if not merged or a > merged[-1][1]:
            merged.append((a, b))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
    return merged


def clip_intervals_halfopen(
    intervals: List[Tuple[float, float]],
    windows: List[Tuple[float, float]],
) -> List[Tuple[float, float]]:
    """把覆盖区间与生效时段做半开区间求交。

    intervals: 左闭右开覆盖区间；windows: 左闭右开生效区间。
    交集仍为左闭右开；end 恰好等于 window end 的接触不产生时长。
    """
    out: List[Tuple[float, float]] = []
    for a, b in intervals:
        for wa, wb in windows:
            lo, hi = max(a, wa), min(b, wb)
            if hi > lo:
                out.append((lo, hi))
    return out
