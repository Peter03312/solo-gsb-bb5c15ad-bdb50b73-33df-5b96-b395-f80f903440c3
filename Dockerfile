FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DZ_DB_PATH=/data/dangerzones.db \
    DZ_HOST=0.0.0.0 \
    DZ_PORT=8080

WORKDIR /srv/app

# 系统依赖：Shapely 2.x 使用自带 libgeos 的 wheel，无需额外 apt 包
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh && mkdir -p /data

VOLUME ["/data"]
EXPOSE 8080

ENTRYPOINT ["docker-entrypoint.sh"]
