"""ShadowFit AI Server entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.config import settings
from app.core.mediapipe_detector import get_detector
from app.grpc.server import start_grpc_server, stop_grpc_server


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_detector()
    start_grpc_server()
    yield
    stop_grpc_server()


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description="MediaPipe pose analysis, DTW sync scoring, and gRPC integration API",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": settings.APP_NAME}
