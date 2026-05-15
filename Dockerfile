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

EXPOSE 8000 8585

# FastAPI(8000) + gRPC(8585)를 한 프로세스에서 함께 실행
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]