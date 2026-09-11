FROM python:3.12-slim

WORKDIR /app

# 시스템 의존성 (OpenCV용)
RUN apt-get update && \
    apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

COPY ai-server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 빌드 컨텍스트는 저장소 루트다(compose: context: . / dockerfile: ai-server/Dockerfile) —
# gRPC 계약 proto/exercise.proto 가 backend 와 한 벌이라 서비스 폴더 밖에 있다.
COPY ai-server/ .
COPY proto/ /proto/
# 커밋된 exercise_pb2*.py 를 쓰지 않고 이미지에서 다시 생성한다 — 계약 원본이 진실이다.
# (커밋본은 로컬 실행·pytest 용이고, CI 가 원본에서 재생성한 것과 같은지 검사한다)
RUN python -m grpc_tools.protoc -I/proto --python_out=. --grpc_python_out=. /proto/exercise.proto

EXPOSE 8000 8001 8002 8585 8586 8587

# 3개 워커를 각자 다른 포트로 띄운다 (entrypoint.sh 참고 — SO_REUSEPORT 공유 포트는 세션별 sticky routing이 안 됨, 2026-08-26 실측)
COPY ai-server/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
CMD ["/entrypoint.sh"]
