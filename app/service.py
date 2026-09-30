"""业务服务：危险区登记、轨迹点批量提交、穿越审计计算。"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from shapely.geometry import Polygon

from .db import Database
from .geometry import GeometryError, build_polygon, merge_intervals, segment_inside_params
from .timeutils import TimeError, clip_intervals_halfopen, merge_windows, parse_utc, validate_window


class ConflictError(Exception):
    """内容冲突（HTTP 409）。"""


class ValidationError(Exception):
    """语义校验失败（HTTP 400）。"""


class NotFoundError(Exception):
    """资源/版本不存在（HTTP 404）。"""


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class AuditService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.min_duration = db.settings.min_duration

    # ------------------------------------------------------------------
    # 危险区登记
    # ------------------------------------------------------------------

    def register_zone(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        zone_id = payload.get("zone_id")
        if not isinstance(zone_id, str) or not zone_id.strip():
            raise ValidationError("zone_id 必须是非空字符串")
        zone_id = zone_id.strip()

        exterior = payload.get("exterior")
        holes = payload.get("holes") or []
        if not isinstance(holes, list):
            raise ValidationError("holes 必须是环列表")
        try:
            polygon = build_polygon(exterior, holes)
        except GeometryError as exc:
            raise ValidationError(str(exc)) from exc

        raw_windows = payload.get("windows")
        if not isinstance(raw_windows, list) or not raw_windows:
            raise ValidationError("windows 必须是非空左闭右开时段列表 [{start, end}]")
        windows: List[Dict[str, Any]] = []
        for idx, w in enumerate(raw_windows):
            if not isinstance(w, dict):
                raise ValidationError(f"windows[{idx}] 必须是对象")
            try:
                start = parse_utc(w.get("start"), field=f"windows[{idx}].start")
                end = parse_utc(w.get("end"), field=f"windows[{idx}].end")
                validate_window(start, end)
            except TimeError as exc:
                raise ValidationError(str(exc)) from exc
            windows.append(
                {
                    "start_epoch": start.timestamp(),
                    "end_epoch": end.timestamp(),
                    "start_iso": _iso_z(start),
                    "end_iso": _iso_z(end),
                }
            )

        now_iso = _iso_z(datetime.now(timezone.utc))
        with self.db.transaction() as conn:
            # 存在性判断与写入在同一立即事务内完成，并发登记不会读到半次提交
            existing = conn.execute(
                "SELECT * FROM zones WHERE zone_id = ?", (zone_id,)
            ).fetchone()
            created = existing is None
            created_at = existing["created_at"] if existing else now_iso
            revision = self.db.insert_revision(conn, zone_id, now_iso)
            exterior_coords = [list(c) for c in polygon.exterior.coords]
            hole_coords = [[list(c) for c in ring.coords] for ring in polygon.interiors]
            # 不可变历史版本（该修订号下的区域配置快照）
            self.db.insert_zone_version(
                conn, revision, zone_id, exterior_coords, hole_coords, windows, created_at
            )
            # 最新配置快照：未指定修订号的现有查询路径继续使用
            if existing:
                conn.execute("DELETE FROM zone_windows WHERE zone_id = ?", (zone_id,))
                conn.execute("DELETE FROM zones WHERE zone_id = ?", (zone_id,))
            self.db.insert_zone(
                conn, zone_id, exterior_coords, hole_coords, windows, created_at
            )

        return {
            "zone_id": zone_id,
            "status": "created" if created else "updated",
            "revision": revision,
            "num_holes": len(polygon.interiors),
            "num_windows": len(windows),
        }

    def get_zone(self, zone_id: str) -> Optional[Dict[str, Any]]:
        # 区域行与生效窗口在同一快照事务内读取，避免读到覆盖更新的中间态
        with self.db.read_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM zones WHERE zone_id = ?", (zone_id,)
            ).fetchone()
            if row is None:
                return None
            rev_row = conn.execute(
                "SELECT MAX(revision) AS r FROM zone_versions WHERE zone_id = ?",
                (zone_id,),
            ).fetchone()
            wrows = conn.execute(
                "SELECT start_iso, end_iso FROM zone_windows WHERE zone_id = ? ORDER BY start_epoch",
                (zone_id,),
            ).fetchall()
        return {
            "zone_id": row["zone_id"],
            "revision": None if rev_row["r"] is None else int(rev_row["r"]),
            "exterior": json.loads(row["exterior"]),
            "holes": json.loads(row["holes"]),
            "windows": [
                {"start": w["start_iso"], "end": w["end_iso"]} for w in wrows
            ],
            "created_at": row["created_at"],
        }

    def list_zones(self) -> List[Dict[str, Any]]:
        # 单次快照读：要么看到某次登记之前、要么之后，绝看不到半次提交
        with self.db.read_transaction() as conn:
            rows = list(conn.execute("SELECT * FROM zones ORDER BY zone_id"))
            result = []
            for row in rows:
                rev_row = conn.execute(
                    "SELECT MAX(revision) AS r FROM zone_versions WHERE zone_id = ?",
                    (row["zone_id"],),
                ).fetchone()
                wrows = conn.execute(
                    "SELECT start_iso, end_iso FROM zone_windows "
                    "WHERE zone_id = ? ORDER BY start_epoch",
                    (row["zone_id"],),
                ).fetchall()
                result.append(
                    {
                        "zone_id": row["zone_id"],
                        "revision": None if rev_row["r"] is None else int(rev_row["r"]),
                        "exterior": json.loads(row["exterior"]),
                        "holes": json.loads(row["holes"]),
                        "windows": [
                            {"start": w["start_iso"], "end": w["end_iso"]} for w in wrows
                        ],
                        "created_at": row["created_at"],
                    }
                )
        return result

    def get_zone_history(self, zone_id: str) -> Optional[List[Dict[str, Any]]]:
        """返回某区域全部不可变历史版本（按修订号升序）。"""
        with self.db.read_transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM zone_versions WHERE zone_id = ? LIMIT 1", (zone_id,)
            ).fetchone()
            if exists is None:
                return None
            rows = list(
                conn.execute(
                    "SELECT v.revision AS revision, v.zone_id AS zone_id, "
                    "v.exterior AS exterior, v.holes AS holes, v.created_at AS created_at, "
                    "r.created_at AS registered_at "
                    "FROM zone_versions v JOIN revisions r ON r.revision = v.revision "
                    "WHERE v.zone_id = ? ORDER BY v.revision",
                    (zone_id,),
                )
            )
            window_rows = {
                int(rev): conn.execute(
                    "SELECT start_iso, end_iso FROM zone_version_windows "
                    "WHERE revision = ? ORDER BY start_epoch",
                    (rev,),
                ).fetchall()
                for rev in [row["revision"] for row in rows]
            }

        result: List[Dict[str, Any]] = []
        for row in rows:
            revision = int(row["revision"])
            result.append(
                {
                    "zone_id": row["zone_id"],
                    "revision": revision,
                    "registered_at": row["registered_at"],
                    "created_at": row["created_at"],
                    "exterior": json.loads(row["exterior"]),
                    "holes": json.loads(row["holes"]),
                    "windows": [
                        {"start": w["start_iso"], "end": w["end_iso"]}
                        for w in window_rows[revision]
                    ],
                }
            )
        return result

    # ------------------------------------------------------------------
    # 轨迹点批量提交（幂等 / 冲突）
    # ------------------------------------------------------------------

    def submit_points(self, trip_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(trip_id, str) or not trip_id.strip():
            raise ValidationError("trip_id 必须是非空字符串")
        trip_id = trip_id.strip()

        points = payload.get("points")
        if not isinstance(points, list) or not points:
            raise ValidationError("points 必须是非空轨迹点列表")

        # ---- 批内解析与校验 ----
        parsed: List[Tuple[str, float, float, float, str]] = []
        # point_id -> (ts,x,y,ts_iso)；ts -> point_id，用于批内一致性检查
        seen_ids: Dict[str, Tuple[float, float, float, str]] = {}
        seen_ts: Dict[float, str] = {}

        for idx, item in enumerate(points):
            if not isinstance(item, dict):
                raise ValidationError(f"points[{idx}] 必须是对象")
            point_id = item.get("point_id")
            if not isinstance(point_id, str) or not point_id.strip():
                raise ValidationError(f"points[{idx}].point_id 必须是非空字符串")
            point_id = point_id.strip()

            try:
                dt = parse_utc(item.get("ts"), field=f"points[{idx}].ts")
            except TimeError as exc:
                raise ValidationError(str(exc)) from exc

            x_raw, y_raw = item.get("x"), item.get("y")
            if isinstance(x_raw, bool) or isinstance(y_raw, bool):
                raise ValidationError(f"points[{idx}] 坐标必须是数值")
            try:
                x, y = float(x_raw), float(y_raw)
            except (TypeError, ValueError):
                raise ValidationError(
                    f"points[{idx}] 坐标必须是数值: x={x_raw!r}, y={y_raw!r}"
                )

            if not (math.isfinite(x) and math.isfinite(y)):
                raise ValidationError(
                    f"points[{idx}] 坐标必须有限，禁止 NaN/Infinity: x={x_raw!r}, y={y_raw!r}"
                )

            ts_epoch = dt.timestamp()
            sig = (ts_epoch, x, y)

            # 同 point_id 批内重复：内容一致幂等，不一致冲突
            if point_id in seen_ids:
                if seen_ids[point_id][:3] != sig:
                    raise ConflictError(
                        f"批内冲突: point_id={point_id!r} 出现了不同内容（时刻或坐标不一致）"
                    )
                continue

            # 同车次同刻不同点
            if ts_epoch in seen_ts:
                raise ConflictError(
                    f"批内冲突: 时刻 {_iso_z(dt)} 同时关联了 point_id="
                    f"{seen_ts[ts_epoch]!r} 与 {point_id!r}"
                )

            seen_ids[point_id] = (ts_epoch, x, y, _iso_z(dt))
            seen_ts[ts_epoch] = point_id
            parsed.append((point_id, ts_epoch, x, y, _iso_z(dt)))

        # ---- 与已存数据比对（只读阶段，先全部收集错误） ----
        stored_ids: Dict[str, Tuple[float, float, float]] = {}
        for point_id, ts_epoch, x, y, _ in parsed:
            row = self.db.get_point(trip_id, point_id)
            if row is not None:
                stored = (row["ts_epoch"], row["x"], row["y"])
                incoming = (ts_epoch, x, y)
                if stored != incoming:
                    raise ConflictError(
                        f"point_id={point_id!r} 已存在且内容不同："
                        f"已存 (ts_epoch={stored[0]}, x={stored[1]}, y={stored[2]})，"
                        f"新传 (ts_epoch={ts_epoch}, x={x}, y={y})"
                    )
                stored_ids[point_id] = incoming
                continue
            # 新点：检查是否与已有同刻点撞车
            other = self.db.find_point_at_ts(trip_id, ts_epoch)
            if other is not None:
                raise ConflictError(
                    f"时刻冲突: point_id={point_id!r} 的时刻与已存 point_id="
                    f"{other['point_id']!r} 相同（同车次同刻只能有一个点）"
                )

        # ---- 单事务写入新点 ----
        new_rows = [
            (trip_id, point_id, ts_epoch, x, y)
            for point_id, ts_epoch, x, y, _ in parsed
            if point_id not in stored_ids
        ]
        with self.db.transaction() as conn:
            self.db.insert_points(conn, new_rows)

        return {
            "trip_id": trip_id,
            "received": len(points),
            "accepted_new": len(new_rows),
            "idempotent_retransmit": len(points) - len(new_rows),
        }

    # ------------------------------------------------------------------
    # 穿越审计
    # ------------------------------------------------------------------

    def query_crossings(
        self,
        trip_id: str,
        zone_id: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        try:
            lo = parse_utc(since, field="since").timestamp() if since else None
            hi = parse_utc(until, field="until").timestamp() if until else None
        except TimeError as exc:
            raise ValidationError(str(exc)) from exc
        if lo is not None and hi is not None and lo >= hi:
            raise ValidationError("since 必须早于 until（查询范围为半开 [since, until)）")

        if revision is not None:
            if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
                raise ValidationError("revision 必须是正整数")

        # 一次只读快照事务内取齐轨迹与区域配置，保证读不到并发写入的半次提交
        with self.db.read_transaction() as conn:
            if revision is not None:
                current = self.db.current_revision(conn)
                if current == 0:
                    raise NotFoundError("尚无任何区域登记，修订号不存在: 0")
                if revision > current:
                    raise NotFoundError(
                        f"修订号超出当前范围: revision={revision}，当前最大修订号={current}"
                    )

            points = self.db.list_trip_points(trip_id, conn)
            if len(points) < 2:
                response = {
                    "trip_id": trip_id,
                    "point_count": len(points),
                    "zones": [],
                }
                if revision is not None:
                    response["revision"] = revision
                return response

            if revision is None:
                # 现有最新配置路径（请求字段与响应语义不变）
                zone_rows = self.db.list_zone_rows(conn)
                if zone_id is not None:
                    zone_rows = [r for r in zone_rows if r["zone_id"] == zone_id]
                    if not zone_rows:
                        raise ValidationError(f"危险区不存在: {zone_id}")
                snapshots = [
                    {
                        "zone_id": zrow["zone_id"],
                        "exterior": json.loads(zrow["exterior"]),
                        "holes": json.loads(zrow["holes"]),
                        "windows": self.db.get_windows(zrow["zone_id"], conn),
                    }
                    for zrow in zone_rows
                ]
            else:
                # 指定修订号：取各区截至该号的最后版本；该版本下未登记的区域不参与
                snapshots = self.db.zone_snapshots_at_revision(conn, revision, zone_id)

        # points 已在 SQL 层按时刻排序，乱序上传在此完成重建

        zone_results: List[Dict[str, Any]] = []
        for snap in snapshots:
            polygon = Polygon(snap["exterior"], snap["holes"])
            windows = merge_windows(snap["windows"])

            result = self._cross_one_zone(points, polygon, windows, lo, hi)
            result["zone_id"] = snap["zone_id"]
            zone_results.append(result)

        response = {
            "trip_id": trip_id,
            "point_count": len(points),
            "zones": zone_results,
        }
        if revision is not None:
            response["revision"] = revision
        return response

    def _cross_one_zone(
        self,
        points: List[Any],
        polygon: Polygon,
        windows: List[Tuple[float, float]],
        lo: Optional[float],
        hi: Optional[float],
    ) -> Dict[str, Any]:
        # 覆盖参数（时间）区间：遍历相邻点，乱序已在 SQL 层按时刻排序重建
        cover: List[Tuple[float, float]] = []
        for a, b in zip(points, points[1:]):
            t0, t1 = a["ts_epoch"], b["ts_epoch"]
            if t1 <= t0:
                # 同刻点不应出现（唯一约束），稳妥跳过
                continue
            params = segment_inside_params(
                polygon, (a["x"], a["y"]), (b["x"], b["y"])
            )
            for pa, pb in params:
                # 参数 0/1 直接取端点时刻，保证相邻段在公共采样点精确相接
                s = t0 if pa == 0.0 else t0 + pa * (t1 - t0)
                e = t1 if pb == 1.0 else t0 + pb * (t1 - t0)
                if e - s > self.min_duration:
                    cover.append((s, e))

        # 时间域合并留 1e-6 秒容差，吸收不同时刻解析路径的浮点误差
        cover = merge_intervals(cover, eps=1e-6)
        intervals = clip_intervals_halfopen(cover, windows)
        if lo is not None or hi is not None:
            bounds = [(-float("inf") if lo is None else lo, float("inf") if hi is None else hi)]
            intervals = clip_intervals_halfopen(intervals, bounds)
        intervals = merge_intervals(intervals, eps=1e-6)
        intervals = [(s, e) for s, e in intervals if e - s > self.min_duration]

        segments = [
            {
                "start": _iso_z(datetime.fromtimestamp(s, timezone.utc)),
                "end": _iso_z(datetime.fromtimestamp(e, timezone.utc)),
                "duration_sec": round(e - s, 6),
            }
            for s, e in intervals
        ]
        total = round(sum(e - s for s, e in intervals), 6)

        return {
            "crossed": bool(intervals),
            "total_duration_sec": total,
            "segments": segments,
        }
