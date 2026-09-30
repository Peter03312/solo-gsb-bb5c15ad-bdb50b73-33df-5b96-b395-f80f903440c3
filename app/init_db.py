"""显式初始化数据库（建表）。容器启动时由 entrypoint 调用，也可单独执行。"""
from __future__ import annotations

from .config import get_settings
from .db import Database


def main() -> None:
    settings = get_settings()
    print(f"[init_db] 使用数据库: {settings.db_path}", flush=True)
    db = Database(settings)
    db.init_schema()
    db.close()
    print("[init_db] 表结构初始化完成", flush=True)


if __name__ == "__main__":
    main()
