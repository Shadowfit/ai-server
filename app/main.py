"""ShadowFit AI Server — FastAPI 진입점."""

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.config import settings
from app.core.mediapipe_detector import get_detector
from app.grpc.server import run_grpc_server, stop_grpc_server
from app.middleware.auth import InternalAuthMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 시작 시 MediaPipe 모델 미리 로드
    get_detector()

    # gRPC 서버를 백그라운드 스레드로 함께 실행
    grpc_thread = threading.Thread(
        target=run_grpc_server, name="grpc-server", daemon=True
    )
    grpc_thread.start()
    logger.info("gRPC 서버 백그라운드 스레드 시작")

    yield

    # 종료 시 gRPC 서버 graceful stop
    stop_grpc_server()


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description="MediaPipe 포즈 감지, DTW 동기화율 계산, 영상 전처리 API",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# 분기 H2 (프론트 → AI 직결) 대응 Bearer 토큰 검증.
# Starlette 는 나중에 add 한 미들웨어를 바깥쪽에 두므로 이 줄이 가장 바깥(요청 진입 시 첫 통과 지점)이 된다.
# OPTIONS preflight 와 /health 등 공개 경로는 미들웨어 내부에서 우회한다.
app.add_middleware(InternalAuthMiddleware)

app.include_router(api_router)


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": settings.APP_NAME}
