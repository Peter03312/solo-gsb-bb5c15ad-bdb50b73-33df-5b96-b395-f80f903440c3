"""HTTP API 集成测试。"""
from tests.conftest import DAY_WINDOWS, SQUARE, register_square


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_register_and_get_zone(client):
    r = register_square(client, zone_id="api-z1")
    assert r.status_code == 201, r.text
    r = client.get("/api/v1/zones/api-z1")
    assert r.status_code == 200
    body = r.json()
    assert body["zone_id"] == "api-z1"
    assert body["windows"][0]["start"].endswith("Z")
    # 外环存储为闭合环
    assert body["exterior"][0] == body["exterior"][-1]


def test_register_self_intersecting(client):
    bowtie = [[0, 0], [100, 100], [100, 0], [0, 100]]
    r = register_square(client, zone_id="bad", exterior=bowtie)
    assert r.status_code == 400
    assert "自交" in r.json()["detail"] or "不合法" in r.json()["detail"]


def test_register_hole_outside(client):
    r = register_square(
        client, zone_id="bad-hole", holes=[[[200, 200], [240, 200], [240, 240]]]
    )
    assert r.status_code == 400


def test_register_naive_time_rejected(client):
    r = client.post(
        "/api/v1/zones",
        json={
            "zone_id": "naive",
            "exterior": SQUARE,
            "holes": [],
            "windows": [{"start": "2026-09-30T00:00:00", "end": "2026-10-01T00:00:00"}],
        },
    )
    assert r.status_code == 400
    assert "时区" in r.json()["detail"]


def test_window_must_be_positive(client):
    r = client.post(
        "/api/v1/zones",
        json={
            "zone_id": "badwin",
            "exterior": SQUARE,
            "windows": [{"start": "2026-10-01T00:00:00Z", "end": "2026-09-30T00:00:00Z"}],
        },
    )
    assert r.status_code == 400


def _crossing_points(start_ts, points):
    return {"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in points]}


BASIC_POINTS = [
    ("p1", "2026-09-30T10:00:00Z", -50, 50),
    ("p2", "2026-09-30T10:00:40Z", 50, 50),
    ("p3", "2026-09-30T10:01:20Z", 150, 50),
]


def test_basic_crossing(client):
    assert register_square(client, zone_id="basic").status_code == 201
    r = client.post("/api/v1/trips/T1/points", json=_crossing_points(None, BASIC_POINTS))
    assert r.status_code == 200, r.text
    r = client.get("/api/v1/trips/T1/crossings")
    zones = r.json()["zones"]
    assert len(zones) == 1
    z = zones[0]
    assert z["crossed"] is True
    # 200m / 80s = 2.5m/s，x=0 在 10:00:20，x=100 在 10:01:00，共 40 秒
    assert z["total_duration_sec"] == 40.0
    seg = z["segments"][0]
    assert seg["start"] == "2026-09-30T10:00:20Z"
    assert seg["end"] == "2026-09-30T10:01:00Z"
    assert seg["duration_sec"] == 40.0


def test_out_of_order_upload_rebuilds(client):
    assert register_square(client, zone_id="ooo").status_code == 201
    shuffled = [BASIC_POINTS[2], BASIC_POINTS[0], BASIC_POINTS[1]]
    r = client.post("/api/v1/trips/T2/points", json=_crossing_points(None, shuffled))
    assert r.status_code == 200
    r = client.get("/api/v1/trips/T2/crossings?zone_id=ooo")
    z = r.json()["zones"][0]
    assert z["total_duration_sec"] == 40.0
    assert z["segments"][0]["start"] == "2026-09-30T10:00:20Z"


def test_window_clipping_halfopen(client):
    # 区域 10:00:20（含）~ 10:00:50（不含）生效
    assert register_square(
        client,
        zone_id="winclip",
        windows=[{"start": "2026-09-30T10:00:20Z", "end": "2026-09-30T10:00:50Z"}],
    ).status_code == 201
    client.post("/api/v1/trips/T3/points", json=_crossing_points(None, BASIC_POINTS))
    r = client.get("/api/v1/trips/T3/crossings?zone_id=winclip")
    z = r.json()["zones"][0]
    assert z["total_duration_sec"] == 30.0
    assert z["segments"][0]["start"] == "2026-09-30T10:00:20Z"
    assert z["segments"][0]["end"] == "2026-09-30T10:00:50Z"


def test_not_effective_no_crossing(client):
    assert register_square(
        client,
        zone_id="future",
        windows=[{"start": "2026-10-02T00:00:00Z", "end": "2026-10-03T00:00:00Z"}],
    ).status_code == 201
    client.post("/api/v1/trips/T4/points", json=_crossing_points(None, BASIC_POINTS))
    z = client.get("/api/v1/trips/T4/crossings?zone_id=future").json()["zones"][0]
    assert z["crossed"] is False
    assert z["total_duration_sec"] == 0
    assert z["segments"] == []


def test_idempotent_retransmit(client):
    assert register_square(client, zone_id="idem").status_code == 201
    body = _crossing_points(None, BASIC_POINTS)
    r1 = client.post("/api/v1/trips/T5/points", json=body)
    assert r1.json()["accepted_new"] == 3
    r2 = client.post("/api/v1/trips/T5/points", json=body)
    assert r2.status_code == 200
    assert r2.json()["accepted_new"] == 0
    assert r2.json()["idempotent_retransmit"] == 3
    # 同批内重复也幂等
    dup = {"points": body["points"] + [body["points"][0]]}
    r3 = client.post("/api/v1/trips/T5/points", json=dup)
    assert r3.status_code == 200
    assert r3.json()["idempotent_retransmit"] == 4


def test_conflict_same_point_different_content(client):
    body = _crossing_points(None, BASIC_POINTS)
    client.post("/api/v1/trips/T6/points", json=body)
    modified = {
        "points": [
            {"point_id": "p1", "ts": "2026-09-30T10:00:00Z", "x": -999, "y": 999}
        ]
    }
    r = client.post("/api/v1/trips/T6/points", json=modified)
    assert r.status_code == 409
    assert "p1" in r.json()["detail"]


def test_conflict_same_ts_different_point(client):
    body = _crossing_points(None, BASIC_POINTS)
    client.post("/api/v1/trips/T7/points", json=body)
    r = client.post(
        "/api/v1/trips/T7/points",
        json={
            "points": [
                {"point_id": "p9", "ts": "2026-09-30T10:00:00Z", "x": 1, "y": 1}
            ]
        },
    )
    assert r.status_code == 409


def test_batch_failure_writes_nothing(client):
    assert register_square(client, zone_id="atom").status_code == 201
    good = [
        ("a1", "2026-09-30T11:00:00Z", 0, 0),
        ("a2", "2026-09-30T11:00:10Z", 10, 10),
    ]
    # 先存一个 a1
    client.post("/api/v1/trips/T8/points", json=_crossing_points(None, good))
    before = client.get("/api/v1/trips/T8").json()["point_count"]
    # 混合：a1 冲突 + 新点 a3 —— 整批拒绝
    mixed = {
        "points": [
            {"point_id": "a1", "ts": "2026-09-30T11:00:00Z", "x": 5, "y": 5},
            {"point_id": "a3", "ts": "2026-09-30T11:00:20Z", "x": 20, "y": 20},
        ]
    }
    r = client.post("/api/v1/trips/T8/points", json=mixed)
    assert r.status_code == 409
    after = client.get("/api/v1/trips/T8").json()["point_count"]
    assert after == before  # a3 未写入


def test_non_finite_coords_rejected(client):
    for val in ("NaN", "Infinity", "-Infinity"):
        r = client.post(
            "/api/v1/trips/T9/points",
            json={"points": [{"point_id": "x", "ts": "2026-09-30T10:00:00Z", "x": val, "y": 1}]},
        )
        assert r.status_code in (400, 422), val


def test_hole_excluded_in_api(client):
    hole = [[[30, 30], [70, 30], [70, 70], [30, 70]]]
    assert register_square(client, zone_id="hole-api", holes=hole).status_code == 201
    pts = [
        ("h1", "2026-09-30T12:00:00Z", 0, 50),
        ("h2", "2026-09-30T12:01:40Z", 100, 50),  # 100s 走 100m
    ]
    client.post("/api/v1/trips/TH/points", json=_crossing_points(None, pts))
    z = client.get("/api/v1/trips/TH/crossings?zone_id=hole-api").json()["zones"][0]
    # 在区域内 60s（0-30 与 70-100），洞内 40s 不计
    assert z["total_duration_sec"] == 60.0
    assert len(z["segments"]) == 2


def test_touching_segments_merge(client):
    # 两段恰好在边界点相接，应合并为一个区间
    assert register_square(client, zone_id="merge").status_code == 201
    pts = [
        ("m1", "2026-09-30T13:00:00Z", -10, 50),
        ("m2", "2026-09-30T13:00:10Z", 0, 50),    # t=10 到达边界
        ("m3", "2026-09-30T13:00:20Z", 10, 50),   # 继续向内
    ]
    client.post("/api/v1/trips/TM/points", json=_crossing_points(None, pts))
    z = client.get("/api/v1/trips/TM/crossings?zone_id=merge").json()["zones"][0]
    assert len(z["segments"]) == 1
    assert z["total_duration_sec"] == 10.0
    assert z["segments"][0]["start"] == "2026-09-30T13:00:10Z"


def test_zero_duration_contact_no_crossing(client):
    assert register_square(client, zone_id="touch").status_code == 201
    pts = [
        ("t1", "2026-09-30T14:00:00Z", -10, 10),
        ("t2", "2026-09-30T14:00:10Z", 10, -10),  # 仅擦过角点 (0,0)
    ]
    client.post("/api/v1/trips/TT/points", json=_crossing_points(None, pts))
    z = client.get("/api/v1/trips/TT/crossings?zone_id=touch").json()["zones"][0]
    assert z["crossed"] is False
    assert z["segments"] == []


def test_multiple_windows_merge(client):
    assert register_square(
        client,
        zone_id="multiwin",
        windows=[
            {"start": "2026-09-30T10:00:20Z", "end": "2026-09-30T10:00:40Z"},
            {"start": "2026-09-30T10:00:40Z", "end": "2026-09-30T10:00:50Z"},
        ],
    ).status_code == 201
    client.post("/api/v1/trips/TW/points", json=_crossing_points(None, BASIC_POINTS))
    z = client.get("/api/v1/trips/TW/crossings?zone_id=multiwin").json()["zones"][0]
    assert z["total_duration_sec"] == 30.0
    # 相接窗口合并 -> 单段
    assert len(z["segments"]) == 1


def test_query_since_until(client):
    assert register_square(client, zone_id="range").status_code == 201
    client.post("/api/v1/trips/TR/points", json=_crossing_points(None, BASIC_POINTS))
    r = client.get(
        "/api/v1/trips/TR/crossings?zone_id=range"
        "&since=2026-09-30T10:00:30Z&until=2026-09-30T10:01:00Z"
    )
    z = r.json()["zones"][0]
    assert z["total_duration_sec"] == 30.0
