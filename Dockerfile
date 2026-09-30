FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/app

# 先装依赖，利用镜像层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8080

# 启动时自动建库建表（等待数据库就绪），随后启动 HTTP 服务
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
