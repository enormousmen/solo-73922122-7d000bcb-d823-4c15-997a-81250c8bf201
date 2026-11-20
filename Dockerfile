FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /srv
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000
VOLUME ["/data"]

# 单进程即可：写操作由 Store 内部锁串行化，崩溃恢复在启动时自动完成
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
