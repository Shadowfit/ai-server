FROM python:3.12-slim

WORKDIR /app

# 시스템 의존성 (OpenCV용)
RUN apt-get update && \
    apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN python -m grpc_tools.protoc -I./app/proto --python_out=. --grpc_python_out=. ./app/proto/exercise.proto

EXPOSE 8000 8001 8002 8585 8586 8587

# 3개 워커를 각자 다른 포트로 띄운다 (entrypoint.sh 참고 — SO_REUSEPORT 공유 포트는 세션별 sticky routing이 안 됨, 2026-08-26 실측)
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
CMD ["/entrypoint.sh"]
