"""pytest 配置：每个测试使用独立临时 SQLite 数据库。"""
import os
import tempfile

import pytest

_tmpdir = tempfile.mkdtemp(prefix="dz-test-")
os.environ["DZ_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["DZ_PORT"] = "18080"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_tables():
    """每个测试前清空数据（保留表结构），保证用例相互隔离。"""
    from app.main import db as _db

    with _db.transaction() as conn:
        conn.execute("DELETE FROM zone_windows")
        conn.execute("DELETE FROM zones")
        conn.execute("DELETE FROM points")
    yield


@pytest.fixture()
def client():
    # 不使用 with：避免 TestClient 退出时触发 shutdown 关闭全局 DB 连接
    return TestClient(app)


SQUARE = [[0, 0], [100, 0], [100, 100], [0, 100]]
DAY_WINDOWS = [{"start": "2026-09-30T00:00:00Z", "end": "2026-10-01T00:00:00Z"}]


def register_square(client, zone_id="z1", exterior=None, holes=None, windows=None):
    return client.post(
        "/api/v1/zones",
        json={
            "zone_id": zone_id,
            "exterior": exterior or SQUARE,
            "holes": holes or [],
            "windows": windows or DAY_WINDOWS,
        },
    )
