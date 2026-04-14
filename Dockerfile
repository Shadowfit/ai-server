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

EXPOSE 8000

# [테스트용] 지금은 FastAPI 대신 gRPC 서버를 실행해봅니다.
CMD ["python", "mock_server.py"]

# [나중에 실전용] FastAPI와 gRPC를 같이 돌릴 때 다시 uvicorn으로 복구
#CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
