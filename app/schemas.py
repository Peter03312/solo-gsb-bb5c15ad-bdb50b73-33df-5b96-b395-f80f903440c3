"""HTTP 请求/响应模型（Pydantic；坐标数值校验在服务层做有限性检查）。"""
from __future__ import annotations

from typing import List

from pydantic import BaseModel, ConfigDict, Field


class CoordModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 允许任意数值（含 inf/nan 透传），有限性在服务层显式拒绝并给出明确报错
    x: float
    y: float


class WindowModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str = Field(..., description="生效开始 UTC ISO 8601（含）")
    end: str = Field(..., description="生效结束 UTC ISO 8601（不含）")


class ZoneCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    zone_id: str = Field(..., min_length=1)
    # 顶点为 [x, y]，首尾点无需重复
    exterior: List[List[float]] = Field(..., min_length=3)
    holes: List[List[List[float]]] = Field(default_factory=list)
    windows: List[WindowModel] = Field(..., min_length=1)


class TrajectoryPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    point_id: str = Field(..., min_length=1)
    ts: str = Field(..., description="UTC ISO 8601，必须带时区")
    x: float
    y: float


class PointBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    points: List[TrajectoryPoint] = Field(..., min_length=1)
