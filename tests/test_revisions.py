"""修订号 / 历史版本复现测试。"""
from concurrent.futures import ThreadPoolExecutor

from tests.conftest import DAY_WINDOWS, SQUARE, register_square

WIN_FULL = DAY_WINDOWS
WIN_SHORT = [{"start": "2026-09-30T10:00:20Z", "end": "2026-09-30T10:00:50Z"}]

BASIC_POINTS = [
    ("p1", "2026-09-30T10:00:00Z", -50, 50),
    ("p2", "2026-09-30T10:00:40Z", 50, 50),
    ("p3", "2026-09-30T10:01:20Z", 150, 50),
]


def _post_points(client, trip, points=BASIC_POINTS):
    return client.post(
        f"/api/v1/trips/{trip}/points",
        json={"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in points]},
    )


def test_register_returns_incrementing_revision(client):
    r1 = register_square(client, zone_id="rev-a")
    assert r1.status_code == 201
    assert r1.json()["revision"] == 1
    assert r1.json()["status"] == "created"

    r2 = register_square(client, zone_id="rev-b")
    assert r2.json()["revision"] == 2

    # 同 ID 覆盖也分配新修订号
    r3 = register_square(client, zone_id="rev-a", windows=WIN_SHORT)
    body3 = r3.json()
    assert body3["revision"] == 3
    assert body3["status"] == "updated"

    listing = client.get("/api/v1/revisions").json()
    assert listing["current_revision"] == 3
    assert [r["revision"] for r in listing["revisions"]] == [1, 2, 3]

    detail = client.get("/api/v1/zones/rev-a").json()
    assert detail["revision"] == 3


def test_as_of_revision_reproduces_zone_config(client):
    # rev 1：全天生效
    assert register_square(client, zone_id="Z", windows=WIN_FULL).json()["revision"] == 1
    # rev 2：同一区覆盖为短时段
    assert register_square(client, zone_id="Z", windows=WIN_SHORT).json()["revision"] == 2
    _post_points(client, "trip-1")

    # 最新（不带 revision）：短时段 30 秒，响应附带当前修订号
    latest = client.get("/api/v1/trips/trip-1/crossings").json()
    assert latest["revision"] == 2
    z = latest["zones"][0]
    assert z["total_duration_sec"] == 30.0

    # 回到 rev 1：全天窗口，40 秒
    r1 = client.get("/api/v1/trips/trip-1/crossings?revision=1").json()
    assert r1["revision"] == 1
    z1 = r1["zones"][0]
    assert z1["zone_id"] == "Z"
    assert z1["total_duration_sec"] == 40.0
    assert z1["segments"][0]["start"] == "2026-09-30T10:00:20Z"
    assert z1["segments"][0]["end"] == "2026-09-30T10:01:00Z"

    # rev 2 复现短窗口
    r2 = client.get("/api/v1/trips/trip-1/crossings?revision=2").json()
    assert r2["zones"][0]["total_duration_sec"] == 30.0

    # since/until 等既有参数在版本化查询中仍然生效
    r1_clip = client.get(
        "/api/v1/trips/trip-1/crossings?revision=1"
        "&since=2026-09-30T10:00:30Z&until=2026-09-30T10:01:00Z"
    ).json()
    assert r1_clip["zones"][0]["total_duration_sec"] == 30.0


def test_geometry_change_reproduced_as_of(client):
    # rev 1：0..100 的方块
    assert register_square(client, zone_id="G").status_code == 201
    # rev 2：同 ID，方块右移到 200..300
    shifted = [[200, 0], [300, 0], [300, 100], [200, 100]]
    r2 = register_square(client, zone_id="G", exterior=shifted)
    assert r2.status_code == 201
    assert r2.json()["status"] == "updated"
    assert r2.json()["revision"] == 2
    _post_points(client, "trip-g")

    r1 = client.get("/api/v1/trips/trip-g/crossings?revision=1&zone_id=G").json()
    assert r1["zones"][0]["total_duration_sec"] == 40.0

    r2 = client.get("/api/v1/trips/trip-g/crossings?revision=2&zone_id=G").json()
    assert r2["zones"][0]["crossed"] is False
    assert r2["zones"][0]["total_duration_sec"] == 0


def test_zone_not_yet_registered_excluded_as_of(client):
    assert register_square(client, zone_id="early").status_code == 201   # rev 1
    assert register_square(client, zone_id="late").status_code == 201    # rev 2
    _post_points(client, "trip-2")

    body = client.get("/api/v1/trips/trip-2/crossings?revision=1").json()
    assert [z["zone_id"] for z in body["zones"]] == ["early"]

    # 显式指定尚不存在的区：明确 404
    resp = client.get("/api/v1/trips/trip-2/crossings?revision=1&zone_id=late")
    assert resp.status_code == 404
    assert "尚未登记" in resp.json()["detail"]

    # rev 2 时两区都在
    body2 = client.get("/api/v1/trips/trip-2/crossings?revision=2").json()
    assert [z["zone_id"] for z in body2["zones"]] == ["early", "late"]


def test_revision_out_of_range_404(client):
    register_square(client, zone_id="only")
    _post_points(client, "trip-3")

    # 超过当前最大修订号
    r = client.get("/api/v1/trips/trip-3/crossings?revision=99")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"
    assert "99" in r.json()["detail"]

    # 库中尚无任何修订
    from app.main import db as _db

    with _db.transaction() as conn:
        conn.execute("DELETE FROM zone_version_windows")
        conn.execute("DELETE FROM zone_versions")
        conn.execute("DELETE FROM revisions")
        conn.execute("DELETE FROM zone_windows")
        conn.execute("DELETE FROM zones")
    r2 = client.get("/api/v1/trips/trip-3/crossings?revision=1")
    assert r2.status_code == 404


def test_revision_zero_rejected(client):
    # Query(ge=1) → FastAPI 422
    r = client.get("/api/v1/trips/x/crossings?revision=0")
    assert r.status_code == 422
    r = client.get("/api/v1/trips/x/crossings?revision=abc")
    assert r.status_code == 422


def test_no_revision_path_keeps_legacy_semantics(client):
    register_square(client, zone_id="legacy")
    _post_points(client, "trip-4")

    # zone_id 不存在仍是 400（沿用旧路径）
    r = client.get("/api/v1/trips/trip-4/crossings?zone_id=missing")
    assert r.status_code == 400

    body = client.get("/api/v1/trips/trip-4/crossings").json()
    assert body["revision"] == 1
    assert body["point_count"] == 3
    assert body["zones"][0]["total_duration_sec"] == 40.0


def test_concurrent_registrations_get_unique_sequential_revisions(client):
    n = 25

    def register(i):
        return register_square(client, zone_id=f"c-{i:02d}").json()["revision"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        revisions = list(pool.map(register, range(n)))

    assert sorted(revisions) == list(range(1, n + 1))
    assert len(set(revisions)) == n
    assert client.get("/api/v1/revisions").json()["current_revision"] == n

    # 每个修订号的历史版本都完整可读（没有半次提交留下的残缺版本）
    listing = client.get("/api/v1/revisions").json()
    for item in listing["revisions"]:
        detail = client.get(f"/api/v1/zones/{item['zone_id']}")
        assert detail.status_code == 200


def test_concurrent_queries_see_consistent_snapshots(client):
    register_square(client, zone_id="snap")  # rev 1
    _post_points(client, "trip-snap")
    errors = []

    def update(_):
        try:
            register_square(client, zone_id="snap", windows=WIN_SHORT)
            register_square(client, zone_id="snap", windows=WIN_FULL)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def query(rev):
        try:
            r = client.get(f"/api/v1/trips/trip-snap/crossings?revision={rev}")
            if r.status_code != 200:
                errors.append((rev, r.status_code, r.text))
                return
            total = r.json()["zones"][0]["total_duration_sec"]
            if total not in (30.0, 40.0):
                errors.append(("unexpected total", total))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(update, i) for i in range(5)]
        futs += [pool.submit(query, 1) for _ in range(10)]
        for f in futs:
            f.result()

    assert errors == []
