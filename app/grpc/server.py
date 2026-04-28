from __future__ import annotations

import logging

from app.config import settings
from app.grpc.session_registry import SessionRegistry

logger = logging.getLogger(__name__)

_server = None
_registry: SessionRegistry | None = None


def get_session_registry() -> SessionRegistry:
    global _registry
    if _registry is None:
        _registry = SessionRegistry()
    return _registry


def build_grpc_server():
    from concurrent import futures

    import grpc
    import exercise_pb2_grpc

    from app.grpc.auth_interceptor import AuthInterceptor
    from app.grpc.exercise_servicer import ExerciseServicer
    from app.grpc.spring_client import SpringGrpcClient
    from app.services.pose_analysis_engine import PoseAnalysisEngine

    registry = get_session_registry()
    spring_client = SpringGrpcClient()
    engine = PoseAnalysisEngine(registry, spring_client)
    servicer = ExerciseServicer(engine, registry)

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=10),
        interceptors=[AuthInterceptor()],
    )
    exercise_pb2_grpc.add_ExerciseServiceServicer_to_server(servicer, server)
    server.add_insecure_port(f"{settings.AI_GRPC_HOST}:{settings.AI_GRPC_PORT}")
    return server


def start_grpc_server() -> None:
    global _server
    if _server is not None:
        return

    try:
        _server = build_grpc_server()
    except ModuleNotFoundError as exc:
        logger.warning("gRPC server disabled because dependency is missing: %s", exc)
        _server = None
        return

    _server.start()
    logger.info(
        "gRPC server started on %s:%s",
        settings.AI_GRPC_HOST,
        settings.AI_GRPC_PORT,
    )


def stop_grpc_server() -> None:
    global _server
    if _server is None:
        return

    _server.stop(grace=2)
    _server = None
