"""InternalAuthMiddleware 단위 테스트.

전체 FastAPI 앱(`app.main`)을 띄우는 대신, MediaPipe 등 무거운 의존성을 피하기 위해
검증 대상 미들웨어만 단독 앱에 부착하고 라우트 1개로 검증한다.
작업 요청서(ai-h2-auth-middleware.md §5) 의 케이스 6종을 모두 커버한다.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.config import settings
from app.middleware.auth import InternalAuthMiddleware

VALID_TOKEN = "test-ai-public-token"

# HTTP 미들웨어가 **거부해야 하는** 값 — gRPC 내부 토큰이다(이슈 #134).
# 둘이 같은 값이던 시절엔 이걸로도 통과했다. test_internal_token_is_rejected 가 그 회귀를 막는다.
INTERNAL_TOKEN = "test-internal-grpc-token"


def _make_client() -> TestClient:
    settings.AI_PUBLIC_TOKEN = VALID_TOKEN
    settings.INTERNAL_API_TOKEN = INTERNAL_TOKEN

    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:8081"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(InternalAuthMiddleware)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/pose")
    async def pose():
        return {"ok": True}

    return TestClient(app)


def test_valid_token_passes():
    client = _make_client()
    res = client.post("/pose", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_invalid_token_rejected():
    client = _make_client()
    res = client.post("/pose", headers={"Authorization": "Bearer wrong-token"})
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid token"


def test_missing_header_rejected():
    client = _make_client()
    res = client.post("/pose")
    assert res.status_code == 401
    assert res.json()["detail"] == "Missing bearer token"


def test_malformed_header_rejected():
    client = _make_client()
    res = client.post("/pose", headers={"Authorization": "NotBearer abc"})
    assert res.status_code == 401
    assert res.json()["detail"] == "Missing bearer token"


def test_public_path_bypasses_auth():
    client = _make_client()
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_cors_preflight_bypasses_auth():
    client = _make_client()
    res = client.options(
        "/pose",
        headers={
            "Origin": "http://localhost:8081",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    assert res.status_code == 200
    # CORS 미들웨어가 응답 헤더를 채워야 한다 (인증에서 안 잘리고 통과한 증거)
    assert res.headers.get("access-control-allow-origin") == "http://localhost:8081"


def test_empty_configured_token_does_not_accept_empty_bearer():
    """AI_PUBLIC_TOKEN 미설정(빈 문자열) 상태에서 빈 Bearer 헤더로 통과하면 안 됨."""
    settings.AI_PUBLIC_TOKEN = ""

    app = FastAPI()
    app.add_middleware(InternalAuthMiddleware)

    @app.post("/pose")
    async def pose():
        return {"ok": True}

    client = TestClient(app)
    try:
        res = client.post("/pose", headers={"Authorization": "Bearer "})
        assert res.status_code == 401
    finally:
        settings.AI_PUBLIC_TOKEN = VALID_TOKEN


def test_internal_token_is_rejected():
    """gRPC 내부 토큰으로는 HTTP 를 통과할 수 없다 — 이슈 #134 회귀 방지.

    이 테스트가 잠그는 것은 «값이 다르다» 가 아니라 **«HTTP 경로가 내부 토큰을 보지
    않는다»** 는 계약이다. 미들웨어가 다시 INTERNAL_API_TOKEN 을 참조하게 되면
    (또는 둘 중 하나로 fallback 하게 되면) 여기서 깨진다.

    깨졌을 때의 의미: 앱 번들에서 추출한 토큰으로 Spring 내부 gRPC 까지 칠 수 있게
    된다 — decisions/ai-auth-token-flow.md §2②.
    """
    client = _make_client()

    res = client.post("/pose", headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"})

    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid token"
