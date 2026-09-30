"""旧库迁移测试：现存区域统一成为修订号 1 的基线版本，不推断迁移前历史。"""
import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.service import AuditService


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def _build_legacy_db(path: Path) -> None:
    """按 v1.0 旧结构直接写入：zones / zone_windows / points（无任何修订表）。"""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE zones (
            zone_id TEXT PRIMARY KEY, exterior TEXT NOT NULL,
            holes TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE zone_windows (
            id INTEGER PRIMARY KEY AUTOINCREMENT, zone_id TEXT NOT NULL,
            start_epoch REAL NOT NULL, end_epoch REAL NOT NULL,
            start_iso TEXT NOT NULL, end_iso TEXT NOT NULL
        );
        CREATE TABLE points (
            trip_id TEXT NOT NULL, point_id TEXT NOT NULL,
            ts_epoch REAL NOT NULL, x REAL NOT NULL, y REAL NOT NULL,
            PRIMARY KEY (trip_id, point_id)
        );
        """
    )
    square = [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]
    conn.execute(
        "INSERT INTO zones VALUES (?,?,?,?)",
        ("legacy-1", json.dumps(square), "[]", "2026-09-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO zones VALUES (?,?,?,?)",
        ("legacy-2", json.dumps(square), "[]", "2026-09-02T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO zone_windows(zone_id, start_epoch, end_epoch, start_iso, end_iso) "
        "VALUES (?,?,?,?,?)",
        (
            "legacy-1",
            _epoch("2026-09-30T00:00:00Z"),
            _epoch("2026-10-01T00:00:00Z"),
            "2026-09-30T00:00:00Z",
            "2026-10-01T00:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO zone_windows(zone_id, start_epoch, end_epoch, start_iso, end_iso) "
        "VALUES (?,?,?,?,?)",
        (
            "legacy-2",
            _epoch("2026-09-30T00:00:00Z"),
            _epoch("2026-10-01T00:00:00Z"),
            "2026-09-30T00:00:00Z",
            "2026-10-01T00:00:00Z",
        ),
    )
    conn.executemany(
        "INSERT INTO points VALUES (?,?,?,?,?)",
        [
            ("t1", "a", _epoch("2026-09-30T10:00:00Z"), -50.0, 50.0),
            ("t1", "b", _epoch("2026-09-30T10:00:40Z"), 50.0, 50.0),
            ("t1", "c", _epoch("2026-09-30T10:01:20Z"), 150.0, 50.0),
        ],
    )
    conn.commit()
    conn.close()


def _settings(path: Path) -> Settings:
    s = Settings()
    s.db_path = path
    return s


def test_legacy_zones_migrate_to_baseline_revision_one():
    tmp = Path(tempfile.mkdtemp(prefix="dz-migrate-")) / "legacy.db"
    _build_legacy_db(tmp)

    db = Database(_settings(tmp))
    try:
        revisions = db.list_revision_rows()
        # 仅一条基线修订事件，全部现存区共享修订号 1
        assert len(revisions) == 1
        assert int(revisions[0]["revision"]) == 1
        assert revisions[0]["kind"] == "baseline_migration"

        versions = db.list_zone_versions_as_of(1)
        assert {r["zone_id"] for r in versions} == {"legacy-1", "legacy-2"}
        assert {int(r["revision"]) for r in versions} == {1}

        svc = AuditService(db)
        result = svc.query_crossings("t1", revision=1)
        assert result["revision"] == 1
        assert {z["zone_id"] for z in result["zones"]} == {"legacy-1", "legacy-2"}
        assert all(z["total_duration_sec"] == 40.0 for z in result["zones"])

        # 迁移后新登记从修订号 2 开始
        resp = svc.register_zone(
            {
                "zone_id": "fresh",
                "exterior": [[0, 0], [10, 0], [10, 10], [0, 10]],
                "holes": [],
                "windows": [
                    {"start": "2026-09-30T00:00:00Z", "end": "2026-10-01T00:00:00Z"}
                ],
            }
        )
        assert resp["revision"] == 2

        # 修订号 1 的历史复现不含迁移后才登记的区
        old = svc.query_crossings("t1", revision=1)
        assert {z["zone_id"] for z in old["zones"]} == {"legacy-1", "legacy-2"}

        # 基线之前没有历史可查
        out_of_range = None
        try:
            svc.query_crossings("t1", revision=3)
        except Exception as exc:  # noqa: BLE001
            out_of_range = exc
        assert out_of_range is not None
    finally:
        db.close()

    # 迁移幂等：重新打开不重复生成基线
    db2 = Database(_settings(tmp))
    try:
        revisions = db2.list_revision_rows()
        assert [int(r["revision"]) for r in revisions] == [1, 2]
    finally:
        db2.close()


def test_empty_legacy_db_starts_revisions_at_one():
    tmp = Path(tempfile.mkdtemp(prefix="dz-migrate-empty-")) / "empty.db"
    # 空的旧结构库
    conn = sqlite3.connect(tmp)
    conn.executescript(
        "CREATE TABLE zones (zone_id TEXT PRIMARY KEY, exterior TEXT, holes TEXT, created_at TEXT);"
    )
    conn.close()

    db = Database(_settings(tmp))
    try:
        assert db.current_revision() is None
        svc = AuditService(db)
        resp = svc.register_zone(
            {
                "zone_id": "first",
                "exterior": [[0, 0], [10, 0], [10, 10], [0, 10]],
                "holes": [],
                "windows": [
                    {"start": "2026-09-30T00:00:00Z", "end": "2026-10-01T00:00:00Z"}
                ],
            }
        )
        assert resp["revision"] == 1
    finally:
        db.close()
