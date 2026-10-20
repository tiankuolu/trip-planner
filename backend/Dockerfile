# syntax=docker/dockerfile:1
# 后端镜像（api 与 worker 共用同一个镜像，用启动命令区分）
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# 默认跑 API；worker 用 command: python -m app.worker 覆盖
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
