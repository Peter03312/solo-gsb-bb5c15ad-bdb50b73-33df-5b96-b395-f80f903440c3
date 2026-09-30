"""SQLite 持久层：危险区、生效时段、轨迹点。

- 所有写操作在单个事务中完成，批量失败不写入部分数据。
- 重启后数据保留（DZ_DB_PATH 指向持久文件）。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .config import Settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS zones (
    zone_id      TEXT PRIMARY KEY,
    exterior     TEXT NOT NULL,          -- GeoJSON-like [[x,y],...]（闭合）
    holes        TEXT NOT NULL,          -- [[[x,y],...], ...]（各环闭合）
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS zone_windows (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id      TEXT NOT NULL REFERENCES zones(zone_id),
    start_epoch  REAL NOT NULL,
    end_epoch    REAL NOT NULL,
    start_iso    TEXT NOT NULL,
    end_iso      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_zone_windows_zone ON zone_windows(zone_id);

CREATE TABLE IF NOT EXISTS points (
    trip_id    TEXT NOT NULL,
    point_id   TEXT NOT NULL,
    ts_epoch   REAL NOT NULL,
    x          REAL NOT NULL,
    y          REAL NOT NULL,
    PRIMARY KEY (trip_id, point_id)
);
-- 同车次同刻必须是同一个点：部分索引防止 (trip, ts) 重复
CREATE UNIQUE INDEX IF NOT EXISTS idx_points_trip_ts
    ON points(trip_id, ts_epoch);
CREATE INDEX IF NOT EXISTS idx_points_trip ON points(trip_id, ts_epoch);
"""


def _connect(settings: Settings) -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.db_path,
        timeout=30,
        isolation_level=None,  # 显式事务
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = _connect(settings)
        self.init_schema()

    def init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    # ----- 危险区 ----------------------------------------------------------

    def get_zone_row(self, zone_id: str) -> Optional[sqlite3.Row]:
        cur = self._conn.execute("SELECT * FROM zones WHERE zone_id = ?", (zone_id,))
        return cur.fetchone()

    def list_zone_rows(self) -> List[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM zones ORDER BY zone_id"))

    def get_windows(self, zone_id: str) -> List[Tuple[float, float]]:
        cur = self._conn.execute(
            "SELECT start_epoch, end_epoch FROM zone_windows WHERE zone_id = ? "
            "ORDER BY start_epoch",
            (zone_id,),
        )
        return [(row["start_epoch"], row["end_epoch"]) for row in cur.fetchall()]

    def insert_zone(
        self,
        conn: sqlite3.Connection,
        zone_id: str,
        exterior: List[List[float]],
        holes: List[List[List[float]]],
        windows: List[Dict[str, Any]],
        created_iso: str,
    ) -> None:
        conn.execute(
            "INSERT INTO zones(zone_id, exterior, holes, created_at) VALUES (?,?,?,?)",
            (zone_id, json.dumps(exterior), json.dumps(holes), created_iso),
        )
        conn.executemany(
            "INSERT INTO zone_windows(zone_id, start_epoch, end_epoch, start_iso, end_iso) "
            "VALUES (?,?,?,?,?)",
            [
                (
                    zone_id,
                    w["start_epoch"],
                    w["end_epoch"],
                    w["start_iso"],
                    w["end_iso"],
                )
                for w in windows
            ],
        )

    # ----- 轨迹点 ----------------------------------------------------------

    def get_point(self, trip_id: str, point_id: str) -> Optional[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM points WHERE trip_id = ? AND point_id = ?",
            (trip_id, point_id),
        )
        return cur.fetchone()

    def find_point_at_ts(self, trip_id: str, ts_epoch: float) -> Optional[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM points WHERE trip_id = ? AND ts_epoch = ?",
            (trip_id, ts_epoch),
        )
        return cur.fetchone()

    def list_trip_points(self, trip_id: str) -> List[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM points WHERE trip_id = ? ORDER BY ts_epoch, point_id",
                (trip_id,),
            )
        )

    def count_trip_points(self, trip_id: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) AS c FROM points WHERE trip_id = ?", (trip_id,)
        )
        return int(cur.fetchone()["c"])

    def insert_points(
        self, conn: sqlite3.Connection, rows: List[Tuple[str, str, float, float, float]]
    ) -> None:
        conn.executemany(
            "INSERT INTO points(trip_id, point_id, ts_epoch, x, y) VALUES (?,?,?,?,?)",
            rows,
        )
