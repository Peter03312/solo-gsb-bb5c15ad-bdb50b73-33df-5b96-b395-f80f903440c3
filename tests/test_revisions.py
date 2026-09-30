"""修订号 / 区域历史版本测试。"""
import threading

import pytest

from tests.conftest import DAY_WINDOWS, SQUARE, register_square


def _points(pairs):
    return {"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in pairs]}


CROSS_POINTS = [
    ("p1", "2026-09-30T10:00:00Z", -50, 50),
    ("p2", "2026-09-30T10:00:40Z", 50, 50),
    ("p3", "2026-09-30T10:01:20Z", 150, 50),
]


def test_register_returns_globally_incrementing_revision(client):
    r1 = register_square(client, zone_id="rev-a")
    assert r1.status_code == 201
    assert r1.json()["revision"] == 1
    r2 = register_square(client, zone_id="rev-b")
    assert r2.json()["revision"] == 2
    # 同 ID 覆盖更新也占用新的全局递增修订号
    r3 = register_square(client, zone_id="rev-a")
    assert r3.status_code == 201  # PUT 语义仍由服务层返回 status
    body3 = r3.json()
    assert body3["status"] == "updated"
    assert body3["revision"] == 3


def test_get_zone_carries_revision(client):
    register_square(client, zone_id="rev-g")
    body = client.get("/api/v1/zones/rev-g").json()
    assert body["revision"] == 1


def test_revision_reproduces_geometry_at_time(client):
    # rev 1：宽 100 的方块（x 0..100），横穿耗时 40s
    register_square(client, zone_id="geo")
    # rev 2：宽 200 的方块（x 0..200）
    register_square(
        client,
        zone_id="geo",
        exterior=[[0, 0], [200, 0], [200, 100], [0, 100]],
    )
    client.post("/api/v1/trips/GT/points", json=_points(CROSS_POINTS))

    latest = client.get("/api/v1/trips/GT/crossings?zone_id=geo").json()
    # 最新配置（0..200）：轨迹止于 x=150（10:01:20），x=0 在 10:00:20 → 60s
    assert "revision" not in latest
    assert latest["zones"][0]["total_duration_sec"] == 60.0

    at1 = client.get("/api/v1/trips/GT/crossings?zone_id=geo&revision=1").json()
    # 修订号 1 的旧配置（0..100）：40s
    assert at1["revision"] == 1
    assert at1["zones"][0]["total_duration_sec"] == 40.0
    assert at1["zones"][0]["segments"][0]["start"] == "2026-09-30T10:00:20Z"
    assert at1["zones"][0]["segments"][0]["end"] == "2026-09-30T10:01:00Z"

    at2 = client.get("/api/v1/trips/GT/crossings?zone_id=geo&revision=2").json()
    assert at2["revision"] == 2
    assert at2["zones"][0]["total_duration_sec"] == 60.0


def test_revision_reproduces_windows_at_time(client):
    # rev 1：全天生效
    register_square(client, zone_id="win")
    # rev 2：只在 10:00:20（含）~ 10:00:50（不含）生效
    register_square(
        client,
        zone_id="win",
        windows=[{"start": "2026-09-30T10:00:20Z", "end": "2026-09-30T10:00:50Z"}],
    )
    client.post("/api/v1/trips/WT/points", json=_points(CROSS_POINTS))

    at1 = client.get("/api/v1/trips/WT/crossings?zone_id=win&revision=1").json()
    assert at1["zones"][0]["total_duration_sec"] == 40.0
    at2 = client.get("/api/v1/trips/WT/crossings?zone_id=win&revision=2").json()
    assert at2["zones"][0]["total_duration_sec"] == 30.0


def test_zone_not_registered_at_revision_excluded(client):
    # rev 1 登记 a
    register_square(client, zone_id="za")
    # rev 2 登记 b
    register_square(client, zone_id="zb")
    client.post("/api/v1/trips/XT/points", json=_points(CROSS_POINTS))

    at1 = client.get("/api/v1/trips/XT/crossings?revision=1").json()
    ids = sorted(z["zone_id"] for z in at1["zones"])
    assert ids == ["za"]

    at2 = client.get("/api/v1/trips/XT/crossings?revision=2").json()
    ids = sorted(z["zone_id"] for z in at2["zones"])
    assert ids == ["za", "zb"]

    # 指定 zone_id 过滤，但该区域在该修订号尚未登记：不参与，结果为空（不报错）
    r = client.get("/api/v1/trips/XT/crossings?zone_id=zb&revision=1")
    assert r.status_code == 200
    assert r.json()["zones"] == []
    assert r.json()["revision"] == 1


def test_as_of_takes_last_version(client):
    register_square(client, zone_id="ao")          # rev 1
    register_square(client, zone_id="other")        # rev 2（别的区）
    client.post("/api/v1/trips/AOT/points", json=_points(CROSS_POINTS))
    # revision=2 时 ao 的截至最后版本仍是 rev 1
    body = client.get("/api/v1/trips/AOT/crossings?zone_id=ao&revision=2").json()
    assert body["zones"][0]["total_duration_sec"] == 40.0


def test_revision_out_of_range_404(client):
    register_square(client, zone_id="rng")
    client.post("/api/v1/trips/RT/points", json=_points(CROSS_POINTS))
    r = client.get("/api/v1/trips/RT/crossings?revision=999")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"
    assert "999" in r.json()["detail"]


def test_revision_zero_or_negative_422(client):
    client.post("/api/v1/trips/ZT/points", json=_points(CROSS_POINTS))
    for val in ("0", "-3"):
        r = client.get(f"/api/v1/trips/ZT/crossings?revision={val}")
        assert r.status_code == 422, val


def test_revision_non_integer_422(client):
    r = client.get("/api/v1/trips/ZT/crossings?revision=1.5")
    assert r.status_code == 422


def test_revision_when_no_zones_registered_404(client):
    client.post("/api/v1/trips/NT/points", json=_points(CROSS_POINTS))
    r = client.get("/api/v1/trips/NT/crossings?revision=1")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_history_endpoint(client):
    register_square(client, zone_id="hist")                          # rev 1
    register_square(
        client,
        zone_id="hist",
        windows=[{"start": "2026-09-30T10:00:00Z", "end": "2026-09-30T11:00:00Z"}],
    )                                                                # rev 2
    r = client.get("/api/v1/zones/hist/history")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    revisions = [v["revision"] for v in body["versions"]]
    assert revisions == [1, 2]
    # 历史版本不可变：rev 1 保留全天窗口
    assert body["versions"][0]["windows"] == DAY_WINDOWS
    assert body["versions"][1]["windows"] == [
        {"start": "2026-09-30T10:00:00Z", "end": "2026-09-30T11:00:00Z"}
    ]
    for v in body["versions"]:
        assert v["exterior"][0] == v["exterior"][-1]
        assert "registered_at" in v

    assert client.get("/api/v1/zones/nope/history").status_code == 404


def test_failed_update_does_not_consume_revision(client):
    r_ok = register_square(client, zone_id="seq")
    assert r_ok.json()["revision"] == 1
    bowtie = [[0, 0], [100, 100], [100, 0], [0, 100]]
    r_bad = register_square(client, zone_id="seq", exterior=bowtie)
    assert r_bad.status_code == 400
    # 失败的登记回滚，不占用修订号：下一次成功仍是 2
    r_next = register_square(client, zone_id="seq")
    assert r_next.json()["revision"] == 2
    # 历史只有两个版本（失败的那次未留下任何痕迹）
    hist = client.get("/api/v1/zones/seq/history").json()
    assert hist["count"] == 2


def test_concurrent_registrations_get_unique_revisions(client):
    errors = []
    revisions = []
    lock = threading.Lock()

    def worker(i):
        try:
            r = register_square(client, zone_id=f"conc-{i}")
            with lock:
                revisions.append(r.json()["revision"])
        except Exception as exc:  # pragma: no cover - 仅用于暴露并发缺陷
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sorted(revisions) == list(range(1, 11))


def test_legacy_zones_migrated_as_baseline(tmp_path):
    """旧库（无版本表）现存区域在首次打开时迁移为基线版本，不推断迁移前历史。"""
    import sqlite3

    from types import SimpleNamespace

    from app.db import Database
    from app.service import AuditService

    db_path = tmp_path / "legacy.db"
    # 手工构造一个"旧库"：只有旧版 zones / zone_windows 表
    legacy_conn = sqlite3.connect(db_path)
    legacy_conn.executescript(
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
            trip_id TEXT NOT NULL, point_id TEXT NOT NULL, ts_epoch REAL NOT NULL,
            x REAL NOT NULL, y REAL NOT NULL, PRIMARY KEY (trip_id, point_id)
        );
        """
    )
    import json
    from datetime import datetime

    def parse(s):
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

    legacy_conn.execute(
        "INSERT INTO zones(zone_id, exterior, holes, created_at) VALUES (?,?,?,?)",
        (
            "legacy-z",
            json.dumps([[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]),
            json.dumps([]),
            "2026-01-01T00:00:00Z",
        ),
    )
    legacy_conn.execute(
        "INSERT INTO zone_windows(zone_id, start_epoch, end_epoch, start_iso, end_iso) "
        "VALUES (?,?,?,?,?)",
        (
            "legacy-z",
            parse("2026-09-30T00:00:00Z"),
            parse("2026-10-01T00:00:00Z"),
            "2026-09-30T00:00:00Z",
            "2026-10-01T00:00:00Z",
        ),
    )
    legacy_conn.commit()
    legacy_conn.close()

    settings = SimpleNamespace(db_path=db_path, min_duration=1e-9)

    db = Database(settings)  # 打开即迁移
    try:
        assert db.current_revision() == 1
        svc = AuditService(db)
        svc.submit_points(
            "legacy-trip",
            {"points": [{"point_id": f"p{i}", "ts": ts, "x": x, "y": 50}
                        for i, (ts, x) in enumerate([
                            ("2026-09-30T10:00:00Z", -50),
                            ("2026-09-30T10:01:20Z", 150),
                        ])]},
        )
        res = svc.query_crossings("legacy-trip", zone_id="legacy-z", revision=1)
        assert res["revision"] == 1
        assert res["zones"][0]["total_duration_sec"] == 40.0
        # 基线之后的新登记获得修订号 2
        reg = svc.register_zone(
            {
                "zone_id": "after",
                "exterior": SQUARE,
                "holes": [],
                "windows": DAY_WINDOWS,
            }
        )
        assert reg["revision"] == 2
        # 迁移幂等：重新打开不会重复迁移
    finally:
        db.close()

    db2 = Database(settings)
    try:
        assert db2.current_revision() == 2
        from app.service import AuditService

        hist = AuditService(db2).get_zone_history("legacy-z")
        assert [v["revision"] for v in hist] == [1]
    finally:
        db2.close()
