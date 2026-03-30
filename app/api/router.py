"""API 라우터 통합."""

from fastapi import APIRouter

from app.api.endpoints import pose, sync, video

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(pose.router)
api_router.include_router(sync.router)
api_router.include_router(video.router)
