"""SQLite 持久层：危险区、生效时段、轨迹点、修订历史。

- 所有写操作在单个事务中完成，批量失败不写入部分数据。
- 每次区域登记/同 ID 覆盖都在同一事务内分配一个全局递增修订号，并写入
  不可变的 zone_versions / zone_version_windows；zones / zone_windows 仅
  保存“当前最新版本”，供未指定修订号的查询沿用原路径。
- 进程内以 RLock 串行化所有连接访问，查询不会读到半次提交；读事务在
  WAL 下取一致性快照，跨进程同样只读到已提交数据。
- 重启后数据保留（DZ_DB_PATH 指向持久文件）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
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

-- 全局修订事件：每次区域登记/同 ID 覆盖一行，revision 全局递增且不复用
CREATE TABLE IF NOT EXISTS revisions (
    revision   INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id    TEXT,                      -- 基线迁移事件为 NULL
    kind       TEXT NOT NULL,             -- register / baseline_migration
    created_at TEXT NOT NULL
);

-- 各区不可变历史版本：一个登记修订号对应一个区；基线修订号由多个现存区共享
CREATE TABLE IF NOT EXISTS zone_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    revision   INTEGER NOT NULL,
    zone_id    TEXT NOT NULL,
    exterior   TEXT NOT NULL,
    holes      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (revision, zone_id)
);
CREATE INDEX IF NOT EXISTS idx_zone_versions_zone ON zone_versions(zone_id, revision);

CREATE TABLE IF NOT EXISTS zone_version_windows (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    revision     INTEGER NOT NULL,
    start_epoch  REAL NOT NULL,
    end_epoch    REAL NOT NULL,
    start_iso    TEXT NOT NULL,
    end_iso      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_zone_version_windows_rev ON zone_version_windows(revision);

-- 结构迁移标记
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
        # 单连接跨线程复用：用可重入锁串行化全部访问，
        # 保证写事务进行期间任何线程都读不到半次提交。
        self._lock = threading.RLock()
        self._conn = _connect(settings)
        self.init_schema()

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._apply_migrations()

    # ------------------------------------------------------------------
    # 迁移
    # ------------------------------------------------------------------

    def _apply_migrations(self) -> None:
        """version=1：旧库现存区域统一迁移为修订号 1 的基线快照。

        仅迁移当时现存的当前配置，不推断迁移前的历史；无区域的新库不
        占用修订号，首次登记仍从 1 开始。整个迁移在一个事务内完成。
        """
        applied = {
            row[0] for row in self._conn.execute("SELECT version FROM schema_migrations")
        }
        if 1 in applied:
            return

        now_iso = _utcnow_iso()
        legacy_rows = list(
            self._conn.execute(
                "SELECT zone_id, exterior, holes, created_at FROM zones ORDER BY zone_id"
            )
        )
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            if legacy_rows:
                # 全部现存区域共享基线修订号 1
                conn.execute(
                    "INSERT INTO revisions(revision, zone_id, kind, created_at) "
                    "VALUES (1, NULL, 'baseline_migration', ?)",
                    (now_iso,),
                )
                for r in legacy_rows:
                    conn.execute(
                        "INSERT INTO zone_versions"
                        "(revision, zone_id, exterior, holes, created_at) "
                        "VALUES (1, ?, ?, ?, ?)",
                        (r["zone_id"], r["exterior"], r["holes"], r["created_at"]),
                    )
                    conn.execute(
                        "INSERT INTO zone_version_windows"
                        "(revision, start_epoch, end_epoch, start_iso, end_iso) "
                        "SELECT 1, start_epoch, end_epoch, start_iso, end_iso "
                        "FROM zone_windows WHERE zone_id = ?",
                        (r["zone_id"],),
                    )
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                (now_iso,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务：持锁 + BEGIN IMMEDIATE，提交前对外不可见。"""
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
        """读事务：WAL 下整段读取同一一致性快照，不夹杂半次提交。"""
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

    # ----- 危险区（当前最新版本） -----------------------------------------

    def get_windows(self, zone_id: str) -> List[Tuple[float, float]]:
        with self._lock:
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

    # ----- 修订号与不可变历史版本 -----------------------------------------

    def insert_revision(
        self,
        conn: sqlite3.Connection,
        zone_id: str,
        created_iso: str,
        kind: str = "register",
    ) -> int:
        """在写事务内分配下一个全局递增修订号（AUTOINCREMENT 保证单调不复用）。"""
        cur = conn.execute(
            "INSERT INTO revisions(zone_id, kind, created_at) VALUES (?,?,?)",
            (zone_id, kind, created_iso),
        )
        return int(cur.lastrowid)

    def insert_zone_version(
        self,
        conn: sqlite3.Connection,
        revision: int,
        zone_id: str,
        exterior: List[List[float]],
        holes: List[List[List[float]]],
        created_iso: str,
    ) -> None:
        conn.execute(
            "INSERT INTO zone_versions(revision, zone_id, exterior, holes, created_at) "
            "VALUES (?,?,?,?,?)",
            (revision, zone_id, json.dumps(exterior), json.dumps(holes), created_iso),
        )

    def insert_zone_version_windows(
        self,
        conn: sqlite3.Connection,
        revision: int,
        windows: List[Dict[str, Any]],
    ) -> None:
        conn.executemany(
            "INSERT INTO zone_version_windows"
            "(revision, start_epoch, end_epoch, start_iso, end_iso) "
            "VALUES (?,?,?,?,?)",
            [
                (
                    revision,
                    w["start_epoch"],
                    w["end_epoch"],
                    w["start_iso"],
                    w["end_iso"],
                )
                for w in windows
            ],
        )

    def current_revision(self) -> Optional[int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(revision) AS m FROM revisions"
            ).fetchone()
            return None if row["m"] is None else int(row["m"])

    def list_revision_rows(self) -> List[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT revision, zone_id, kind, created_at FROM revisions "
                    "ORDER BY revision"
                )
            )

    def list_zone_versions_as_of(self, revision: int) -> List[sqlite3.Row]:
        """各区截至给定修订号的最后一个不可变版本（该修订号之后才登记的区不含）。"""
        with self._lock:
            return list(
                self._conn.execute(
                    """
                    SELECT zv.revision  AS revision,
                           zv.zone_id   AS zone_id,
                           zv.exterior  AS exterior,
                           zv.holes     AS holes,
                           zv.created_at AS created_at
                    FROM zone_versions zv
                    JOIN (
                        SELECT zone_id, MAX(revision) AS max_revision
                        FROM zone_versions
                        WHERE revision <= ?
                        GROUP BY zone_id
                    ) latest
                      ON latest.zone_id = zv.zone_id
                     AND latest.max_revision = zv.revision
                    ORDER BY zv.zone_id
                    """,
                    (revision,),
                )
            )

    def get_version_windows(self, revision: int) -> List[Tuple[float, float]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT start_epoch, end_epoch FROM zone_version_windows "
                "WHERE revision = ? ORDER BY start_epoch",
                (revision,),
            )
            return [(row["start_epoch"], row["end_epoch"]) for row in cur.fetchall()]

    # ----- 轨迹点 ----------------------------------------------------------

    def get_point(self, trip_id: str, point_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM points WHERE trip_id = ? AND point_id = ?",
                (trip_id, point_id),
            )
            return cur.fetchone()

    def find_point_at_ts(self, trip_id: str, ts_epoch: float) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM points WHERE trip_id = ? AND ts_epoch = ?",
                (trip_id, ts_epoch),
            )
            return cur.fetchone()

    def list_trip_points(self, trip_id: str) -> List[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM points WHERE trip_id = ? ORDER BY ts_epoch, point_id",
                    (trip_id,),
                )
            )

    def count_trip_points(self, trip_id: str) -> int:
        with self._lock:
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
