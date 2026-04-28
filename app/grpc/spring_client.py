from __future__ import annotations

import logging

import exercise_pb2
from app.config import settings

logger = logging.getLogger(__name__)


class SpringGrpcClient:
    """Small client wrapper for AI -> Spring callbacks."""

    def __init__(self):
        self._target = f"{settings.SPRING_GRPC_HOST}:{settings.SPRING_GRPC_PORT}"

    def _metadata(self) -> list[tuple[str, str]]:
        token = f"Bearer {settings.INTERNAL_API_TOKEN}"
        return [("Authorization", token), ("authorization", token)]

    def save_pose_data_batch(self, session_id: int, pose_batch: list[exercise_pb2.PoseDataRequest]) -> None:
        if not pose_batch:
            return

        request = exercise_pb2.PoseDataBatchRequest(
            session_id=session_id,
            pose_data=pose_batch,
        )
        self._call(
            "SavePoseDataBatch",
            lambda stub: stub.SavePoseDataBatch(
                request,
                timeout=settings.GRPC_CALLBACK_TIMEOUT_SEC,
                metadata=self._metadata(),
            ),
        )

    def complete_analysis(self, final_result: exercise_pb2.SessionCompleteRequest) -> None:
        self._call(
            "CompleteAnalysis",
            lambda stub: stub.CompleteAnalysis(
                final_result,
                timeout=settings.GRPC_CALLBACK_TIMEOUT_SEC,
                metadata=self._metadata(),
            ),
        )

    def _call(self, method_name: str, invoke):
        try:
            import grpc
            import exercise_pb2_grpc

            with grpc.insecure_channel(self._target) as channel:
                stub = exercise_pb2_grpc.ExerciseServiceStub(channel)
                invoke(stub)
        except grpc.RpcError as exc:
            logger.warning(
                "Spring gRPC callback %s failed: %s",
                method_name,
                exc.details() or exc,
            )
