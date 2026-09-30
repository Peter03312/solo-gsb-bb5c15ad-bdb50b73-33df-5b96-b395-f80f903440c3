"""FastAPI 入口：工地作业车危险区穿越审计服务。"""
from __future__ import annotations

from typing import Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .config import get_settings
from .db import Database
from .schemas import PointBatch, ZoneCreate
from .service import AuditService, ConflictError, NotFoundError, ValidationError

settings = get_settings()
db = Database(settings)
service = AuditService(db)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    db.close()


app = FastAPI(
    title="工地作业车危险区穿越审计服务",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "登记带洞米制多边形危险区及其 UTC 半开生效时段；批量提交车辆轨迹点，"
        "查询各危险区的穿越区间与累计时长。"
    ),
)


@app.exception_handler(ValidationError)
async def _validation_handler(_: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": "validation_error", "detail": str(exc)})


@app.exception_handler(ConflictError)
async def _conflict_handler(_: Request, exc: ConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "conflict", "detail": str(exc)})


@app.exception_handler(NotFoundError)
async def _not_found_handler(_: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> dict:
    return {
        "service": "danger-zone-crossing-audit",
        "docs": "/docs",
        "health": "/health",
        "endpoints": [
            "POST /api/v1/zones",
            "GET  /api/v1/zones",
            "GET  /api/v1/zones/{zone_id}",
            "GET  /api/v1/zones/{zone_id}/history",
            "POST /api/v1/trips/{trip_id}/points",
            "GET  /api/v1/trips/{trip_id}",
            "GET  /api/v1/trips/{trip_id}/crossings",
        ],
    }


@app.post("/api/v1/zones", status_code=201)
def create_zone(body: ZoneCreate) -> dict:
    return service.register_zone(body.model_dump())


@app.get("/api/v1/zones")
def list_zones() -> dict:
    return {"zones": service.list_zones()}


@app.get("/api/v1/zones/{zone_id}")
def get_zone(zone_id: str) -> JSONResponse:
    detail = service.get_zone(zone_id)
    if detail is None:
        return JSONResponse(
            status_code=404, content={"error": "not_found", "detail": f"危险区不存在: {zone_id}"}
        )
    return JSONResponse(content=detail)


@app.get("/api/v1/zones/{zone_id}/history")
def get_zone_history(zone_id: str) -> JSONResponse:
    history = service.get_zone_history(zone_id)
    if history is None:
        return JSONResponse(
            status_code=404, content={"error": "not_found", "detail": f"危险区不存在: {zone_id}"}
        )
    return JSONResponse(
        content={"zone_id": zone_id, "count": len(history), "versions": history}
    )


@app.post("/api/v1/trips/{trip_id}/points")
def submit_points(trip_id: str, body: PointBatch) -> dict:
    result = service.submit_points(trip_id, body.model_dump())
    return result


@app.get("/api/v1/trips/{trip_id}")
def get_trip(trip_id: str) -> JSONResponse:
    rows = db.list_trip_points(trip_id)
    if not rows:
        return JSONResponse(
            status_code=404, content={"error": "not_found", "detail": f"车次无轨迹数据: {trip_id}"}
        )
    from datetime import datetime, timezone

    return JSONResponse(
        content={
            "trip_id": trip_id,
            "point_count": len(rows),
            "points": [
                {
                    "point_id": r["point_id"],
                    "ts": datetime.fromtimestamp(r["ts_epoch"], timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "x": r["x"],
                    "y": r["y"],
                }
                for r in rows
            ],
        }
    )


@app.get("/api/v1/trips/{trip_id}/crossings")
def get_crossings(
    trip_id: str,
    zone_id: Optional[str] = Query(default=None),
    since: Optional[str] = Query(default=None, description="只统计该 UTC 时刻（含）之后"),
    until: Optional[str] = Query(default=None, description="只统计该 UTC 时刻（不含）之前"),
    revision: Optional[int] = Query(
        default=None,
        ge=1,
        description="指定全局修订号：按各区截至该号的最后版本核算；不填则使用最新配置",
    ),
) -> dict:
    return service.query_crossings(
        trip_id, zone_id=zone_id, since=since, until=until, revision=revision
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
