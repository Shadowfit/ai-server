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


def run_grpc_server() -> None:
    """블로킹 호출. 백그라운드 스레드에서 실행할 것."""
    global _server

    if not settings.INTERNAL_API_TOKEN:
        raise RuntimeError("INTERNAL_API_TOKEN 환경변수가 설정되지 않았습니다.")

    with _server_lock:
        _server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=10),
            interceptors=[AuthInterceptor(settings.INTERNAL_API_TOKEN)],
        )
        exercise_pb2_grpc.add_ExerciseServiceServicer_to_server(
            ExerciseServicer(), _server
        )
        _server.add_insecure_port(f"[::]:{settings.AI_GRPC_PORT}")
        _server.start()
        logger.info("ShadowFit AI gRPC Server 시작 (port=%d)", settings.AI_GRPC_PORT)

    _server.wait_for_termination()


def stop_grpc_server(grace: float = 3.0) -> None:
    with _server_lock:
        if _server is not None:
            _server.stop(grace)
            logger.info("ShadowFit AI gRPC Server 종료")