"""运行配置：全部通过环境变量覆盖。"""
from __future__ import annotations

import os
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    def __init__(self) -> None:
        self.db_path: Path = Path(os.getenv("DZ_DB_PATH", "./data/dangerzones.db"))
        self.host: str = os.getenv("DZ_HOST", "0.0.0.0")
        self.port: int = int(os.getenv("DZ_PORT", "8080"))
        self.echo_sql: bool = _env_bool("DZ_ECHO_SQL", False)
        # 小于该秒数的覆盖片段视为零时长接触，不计入穿越
        self.min_duration: float = float(os.getenv("DZ_MIN_DURATION_SEC", "1e-9"))


def get_settings() -> Settings:
    return Settings()
