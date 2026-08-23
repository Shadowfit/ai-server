"""FastAPI 측 gRPC 서버 구동.

- 들어오는 호출(Spring → FastAPI) 인증 인터셉터
- 백그라운드 스레드용 run_grpc_server / stop_grpc_server
"""

from __future__ import annotations

import logging
import threading
from concurrent import futures

import grpc

import exercise_pb2_grpc
from app.config import settings
from app.grpc.correlation import CorrelationServerInterceptor
from app.grpc.exercise_servicer import ExerciseServicer

logger = logging.getLogger(__name__)


class AuthInterceptor(grpc.ServerInterceptor):
    """Spring의 InternalAuthInterceptor와 대칭. 'Bearer <token>' 미일치 시 차단."""

    def __init__(self, token: str) -> None:
        self._expected = f"Bearer {token}"

        def abort(ignored_request, context):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "유효하지 않은 토큰")

        self._abort_handler = grpc.unary_unary_rpc_method_handler(abort)

    def intercept_service(self, continuation, handler_call_details):
        metadata = dict(handler_call_details.invocation_metadata)
        if metadata.get("authorization") != self._expected:
            return self._abort_handler
        return continuation(handler_call_details)


_server: grpc.Server | None = None
_server_lock = threading.Lock()

# 🔴 기동 결과를 남긴다 (#430).
#
# gRPC 서버는 **데몬 스레드**에서 돈다(`main.py` lifespan). 스레드가 죽어도 프로세스는 살고,
# 예전에는 그 사실이 어디에도 안 남아 `/health` 가 200 을 계속 냈다 — 컨테이너는 `healthy`,
# HTTP 는 정상, 그런데 Spring 은 `Connection refused` 를 받고 세션이 전부 FAILED 로 떨어진다.
# 겉으로 드러나는 얼굴이 원인과 전혀 안 닮아서, 실제로 2026-08-23 에 세 판을 여기 태웠다.
#
# 그래서 «시작했다» 를 포트가 열린 뒤에만 기록하고, 실패는 사유를 남긴다.
_serving = False
_start_error: str | None = None


def grpc_status() -> tuple[bool, str | None]:
    """(서빙 중인가, 실패 사유). `/health` 가 이것을 그대로 반영한다 (#430)."""
    return _serving, _start_error


def run_grpc_server() -> None:
    """블로킹 호출. 백그라운드 스레드에서 실행할 것."""
    global _server, _serving, _start_error

    try:
        _run_grpc_server_inner()
    except BaseException as e:
        # 사유를 남기고 **그대로 다시 던진다** — 로그의 스택트레이스는 지금도 유일한 단서다.
        # 여기서 삼키면 «죽었는데 조용한» 상태가 하나 더 생긴다.
        _serving = False
        _start_error = f"{type(e).__name__}: {e}"
        raise


def _run_grpc_server_inner() -> None:
    global _server, _serving

    if not settings.INTERNAL_API_TOKEN:
        raise RuntimeError("INTERNAL_API_TOKEN 환경변수가 설정되지 않았습니다.")

    with _server_lock:
        _server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=10),
            # correlation 인터셉터를 앞에 둬서 인증 거부된 호출의 로그에도 id 가 남게 한다.
            interceptors=[
                CorrelationServerInterceptor(),
                AuthInterceptor(settings.INTERNAL_API_TOKEN),
            ],
        )
        exercise_pb2_grpc.add_ExerciseServiceServicer_to_server(
            ExerciseServicer(), _server
        )
        _server.add_insecure_port(f"[::]:{settings.AI_GRPC_PORT}")
        _server.start()
        # 포트가 실제로 열린 **뒤에** 참으로 만든다 — 그 전에 세우면 «시작했다고 적어놓고
        # 안 열린» 창이 생겨, 고치려던 거짓 healthy 를 다시 만든다.
        _serving = True
        logger.info("ShadowFit AI gRPC Server 시작 (port=%d)", settings.AI_GRPC_PORT)

    _server.wait_for_termination()


def stop_grpc_server(grace: float = 3.0) -> None:
    global _serving
    with _server_lock:
        # 정상 종료도 «서빙 아님» 이다 (#430). 안 내리면 종료 중인 서버가 /health 로는
        # 계속 ok 를 내고, 그건 이 변경이 없애려던 거짓말과 같은 종류다.
        _serving = False
        if _server is not None:
            _server.stop(grace)
            logger.info("ShadowFit AI gRPC Server 종료")