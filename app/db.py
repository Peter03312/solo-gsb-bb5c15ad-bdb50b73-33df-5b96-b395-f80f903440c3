"""SQLite 持久层：危险区（含不可变历史版本）、生效时段、轨迹点。

- 所有写操作在单个事务中完成，批量失败不写入部分数据。
- 每次区域登记/同 ID 覆盖更新在同一事务内原子地占用一个全局递增修订号，
  并写入一份不可变的区域快照；查询侧永远读不到半次提交。
- 旧库（只有 zones/zone_windows 两张表的数据）在首次打开时迁移为基线版本
  （修订号 1..N，不推断迁移前历史）。
- 重启后数据保留（DZ_DB_PATH 指向持久文件）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .config import Settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS revisions (
    revision   INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_at_epoch REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS zone_versions (
    revision   INTEGER PRIMARY KEY,
    zone_id    TEXT NOT NULL,
    exterior   TEXT NOT NULL,          -- GeoJSON-like [[x,y],...]（闭合）
    holes      TEXT NOT NULL,          -- [[[x,y],...], ...]（各环闭合）
    created_at TEXT NOT NULL,
    FOREIGN KEY (revision) REFERENCES revisions(revision)
);
CREATE INDEX IF NOT EXISTS idx_zone_versions_zone
    ON zone_versions(zone_id, revision);

CREATE TABLE IF NOT EXISTS zone_version_windows (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    revision     INTEGER NOT NULL,
    zone_id      TEXT NOT NULL,
    start_epoch  REAL NOT NULL,
    end_epoch    REAL NOT NULL,
    start_iso    TEXT NOT NULL,
    end_iso      TEXT NOT NULL,
    FOREIGN KEY (revision) REFERENCES zone_versions(revision)
);
CREATE INDEX IF NOT EXISTS idx_zvw_revision ON zone_version_windows(revision);

-- 最新配置快照表：无修订号的现有查询路径继续使用，与历史版本在同一事务内更新
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
        # 单连接 + 进程内锁：串行化并发事务，保证读事务拿到一致快照、
        # 写事务的修订号分配与版本落库原子完成，不会读到半次提交
        self._lock = threading.RLock()
        self._conn = _connect(settings)
        self.init_schema()
        self._migrate_legacy_zones()

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """立即写事务（BEGIN IMMEDIATE）。同一进程内串行化。"""
        conn = self._conn
        with self._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        """只读快照事务：事务内多次读取看到同一版本，不被并发写入撕裂。"""
        conn = self._conn
        with self._lock:
            conn.execute("BEGIN")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    # ----- 迁移 -----------------------------------------------------------

    def _migrate_legacy_zones(self) -> None:
        """旧库现存区域迁移为基线版本（修订号从 1 起），不推断迁移前历史。

        用 PRAGMA user_version=1 作为"已迁移"标记，可重复执行（幂等）。
        迁移在单事务内完成：要么全部区域成为基线版本，要么不动。
        """
        with self._lock:
            user_version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if user_version >= 1:
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                legacy = list(self._conn.execute("SELECT * FROM zones ORDER BY zone_id"))
                if legacy:
                    now_iso = _utc_now_iso()
                    for row in legacy:
                        cur = self._conn.execute(
                            "INSERT INTO revisions(zone_id, created_at, created_at_epoch) "
                            "VALUES (?,?,?)",
                            (row["zone_id"], now_iso, _iso_to_epoch(now_iso)),
                        )
                        revision = int(cur.lastrowid)
                        self._conn.execute(
                            "INSERT INTO zone_versions(revision, zone_id, exterior, holes, created_at) "
                            "VALUES (?,?,?,?,?)",
                            (
                                revision,
                                row["zone_id"],
                                row["exterior"],
                                row["holes"],
                                row["created_at"],
                            ),
                        )
                        self._conn.execute(
                            "INSERT INTO zone_version_windows"
                            "(revision, zone_id, start_epoch, end_epoch, start_iso, end_iso) "
                            "SELECT ?, zone_id, start_epoch, end_epoch, start_iso, end_iso "
                            "FROM zone_windows WHERE zone_id = ?",
                            (revision, row["zone_id"]),
                        )
                self._conn.execute("PRAGMA user_version=1")
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ----- 修订号 / 版本 ---------------------------------------------------

    def current_revision(self, conn: Optional[sqlite3.Connection] = None) -> int:
        c = conn or self._conn
        row = c.execute("SELECT MAX(revision) AS r FROM revisions").fetchone()
        return int(row["r"] or 0)

    def insert_revision(
        self,
        conn: sqlite3.Connection,
        zone_id: str,
        created_iso: str,
    ) -> int:
        """占用下一个全局递增修订号（必须在写事务内调用）。"""
        cur = conn.execute(
            "INSERT INTO revisions(zone_id, created_at, created_at_epoch) VALUES (?,?,?)",
            (zone_id, created_iso, _iso_to_epoch(created_iso)),
        )
        return int(cur.lastrowid)

    def insert_zone_version(
        self,
        conn: sqlite3.Connection,
        revision: int,
        zone_id: str,
        exterior: List[List[float]],
        holes: List[List[List[float]]],
        windows: List[Dict[str, Any]],
        created_iso: str,
    ) -> None:
        conn.execute(
            "INSERT INTO zone_versions(revision, zone_id, exterior, holes, created_at) "
            "VALUES (?,?,?,?,?)",
            (revision, zone_id, json.dumps(exterior), json.dumps(holes), created_iso),
        )
        conn.executemany(
            "INSERT INTO zone_version_windows"
            "(revision, zone_id, start_epoch, end_epoch, start_iso, end_iso) "
            "VALUES (?,?,?,?,?,?)",
            [
                (
                    revision,
                    zone_id,
                    w["start_epoch"],
                    w["end_epoch"],
                    w["start_iso"],
                    w["end_iso"],
                )
                for w in windows
            ],
        )

    # ----- 最新配置快照 ----------------------------------------------------

    def list_zone_rows(self, conn: Optional[sqlite3.Connection] = None) -> List[sqlite3.Row]:
        c = conn or self._conn
        return list(c.execute("SELECT * FROM zones ORDER BY zone_id"))

    def get_windows(
        self, zone_id: str, conn: Optional[sqlite3.Connection] = None
    ) -> List[Tuple[float, float]]:
        c = conn or self._conn
        cur = c.execute(
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

    # ----- 截至某修订号的版本快照 ------------------------------------------

    def zone_snapshots_at_revision(
        self, conn: sqlite3.Connection, revision: int, zone_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """取各区截至 revision 的最后版本（last-version-as-of）。

        指定版本中尚未登记的区域不参与核算（不出现在结果中）。
        所有读取必须在同一个只读/写事务的 conn 上，保证快照一致。
        """
        sql = (
            "SELECT v.revision AS revision, v.zone_id AS zone_id, "
            "v.exterior AS exterior, v.holes AS holes "
            "FROM zone_versions v "
            "JOIN (SELECT zone_id, MAX(revision) AS max_rev FROM zone_versions "
            "      WHERE revision <= ? GROUP BY zone_id) m "
            "  ON m.zone_id = v.zone_id AND m.max_rev = v.revision"
        )
        params: Tuple[Any, ...] = (revision,)
        if zone_id is not None:
            sql += " WHERE v.zone_id = ?"
            params = (revision, zone_id)
        sql += " ORDER BY v.zone_id"

        snapshots: List[Dict[str, Any]] = []
        for row in conn.execute(sql, params).fetchall():
            wrows = conn.execute(
                "SELECT start_epoch, end_epoch FROM zone_version_windows "
                "WHERE revision = ? ORDER BY start_epoch",
                (row["revision"],),
            ).fetchall()
            snapshots.append(
                {
                    "zone_id": row["zone_id"],
                    "revision": int(row["revision"]),
                    "exterior": json.loads(row["exterior"]),
                    "holes": json.loads(row["holes"]),
                    "windows": [(w["start_epoch"], w["end_epoch"]) for w in wrows],
                }
            )
        return snapshots

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

    def list_trip_points(
        self, trip_id: str, conn: Optional[sqlite3.Connection] = None
    ) -> List[sqlite3.Row]:
        c = conn or self._conn
        return list(
            c.execute(
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


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_to_epoch(iso: str) -> float:
    from datetime import datetime

    text = iso[:-1] + "+00:00" if iso.endswith(("Z", "z")) else iso
    return datetime.fromisoformat(text).timestamp()
