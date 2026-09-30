#!/bin/sh
set -e

echo "[entrypoint] 初始化数据库..."
python -m app.init_db

echo "[entrypoint] 启动审计服务 ${DZ_HOST:-0.0.0.0}:${DZ_PORT:-8080}"
exec uvicorn app.main:app --host "${DZ_HOST:-0.0.0.0}" --port "${DZ_PORT:-8080}"
