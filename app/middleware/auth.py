"""Bearer-token 인증 미들웨어.

분기 H2 (프론트 → AI 직결) 채택으로 `POST /pose` 가 외부 노출되므로
모든 인입 HTTP 요청에 `Authorization: Bearer <INTERNAL_API_TOKEN>` 헤더를 강제한다.
gRPC 측 `AuthInterceptor` (app/grpc/auth_interceptor.py) 와 같은 토큰을 검증한다.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.config import settings

# 인증 우회 경로 — 헬스체크, Swagger 문서, OpenAPI 스펙
PUBLIC_PATHS: frozenset[str] = frozenset(
    {"/health", "/docs", "/redoc", "/openapi.json"}
)


class InternalAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, public_paths: frozenset[str] = PUBLIC_PATHS):
        super().__init__(app)
        self._public_paths = public_paths

    async def dispatch(self, request: Request, call_next) -> Response:
        # CORS preflight (OPTIONS) 와 공개 경로는 인증 우회
        # CORSMiddleware 가 먼저 처리해 여기까지 안 오는 게 정상이지만 방어적으로 둠
        if request.method == "OPTIONS" or request.url.path in self._public_paths:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401, content={"detail": "Missing bearer token"}
            )

        token = auth_header[len("Bearer ") :]
        if not settings.INTERNAL_API_TOKEN or token != settings.INTERNAL_API_TOKEN:
            return JSONResponse(
                status_code=401, content={"detail": "Invalid token"}
            )

        return await call_next(request)
