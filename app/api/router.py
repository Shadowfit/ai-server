"""API 라우터 통합."""

from fastapi import APIRouter

from app.api.endpoints import pose, sync, video
from app.observability import frame_path

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(pose.router)
api_router.include_router(sync.router)
api_router.include_router(video.router)
# 프레임 경로 계측 읽기(§12). 계측이 꺼져 있어도 라우트는 산다 — «꺼져 있음» 을 확인하는
# 것도 측정 절차의 일부다. /pose 와 같은 인증을 탄다.
api_router.include_router(frame_path.router)
