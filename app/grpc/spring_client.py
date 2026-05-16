"""Spring 백엔드로 콜백을 보내는 gRPC 클라이언트.

ExerciseServicer가 rep 완성·세션 종료 시점에 호출한다.
"""

from __future__ import annotations

import logging
import threading

import grpc

import exercise_pb2
import exercise_pb2_grpc
from app.config import settings

logger = logging.getLogger(__name__)

_channel: grpc.Channel | None = None
_stub: exercise_pb2_grpc.ExerciseServiceStub | None = None
_lock = threading.Lock()


def auth_metadata() -> tuple[tuple[str, str], ...]:
    return (("authorization", f"Bearer {settings.INTERNAL_API_TOKEN}"),)


def get_stub() -> exercise_pb2_grpc.ExerciseServiceStub:
    global _channel, _stub
    with _lock:
        if _stub is None:
            _channel = grpc.insecure_channel(settings.BACKEND_GRPC_ADDRESS)
            _stub = exercise_pb2_grpc.ExerciseServiceStub(_channel)
            logger.info("Spring gRPC 채널 생성: %s", settings.BACKEND_GRPC_ADDRESS)
        return _stub


def report_pose_data_batch(
    session_id: int, pose_data_list: list[exercise_pb2.PoseDataRequest]
) -> None:
    """rep 1회 완성 시 Spring에 PoseData 묶음 전송."""
    try:
        request = exercise_pb2.PoseDataBatchRequest(
            session_id=session_id,
            pose_data=pose_data_list,
        )
        response = get_stub().SavePoseDataBatch(request, metadata=auth_metadata())
        logger.info(
            "[AI → Spring] PoseData 배치 전송 (session=%s, count=%d, success=%s)",
            session_id,
            len(pose_data_list),
            response.success,
        )
    except grpc.RpcError as e:
        logger.error("[AI → Spring] PoseData 배치 전송 실패: %s", e.details())


def report_complete_analysis(
    session_id: int,
    total_reps: int,
    avg_sync_rate: float,
    max_sync_rate: float = 0.0,
    min_sync_rate: float = 0.0,
    calories_burned: float = 0.0,
) -> None:
    """최종 분석 결과를 Spring에 콜백."""
    try:
        request = exercise_pb2.SessionCompleteRequest(
            session_id=session_id,
            total_reps=total_reps,
            avg_sync_rate=avg_sync_rate,
            max_sync_rate=max_sync_rate,
            min_sync_rate=min_sync_rate,
            calories_burned=calories_burned,
        )
        response = get_stub().CompleteAnalysis(request, metadata=auth_metadata())
        logger.info(
            "[AI → Spring] CompleteAnalysis (session=%s, status=%s)",
            session_id,
            response.status,
        )
    except grpc.RpcError as e:
        logger.error("[AI → Spring] CompleteAnalysis 실패: %s", e.details())