from __future__ import annotations

import logging

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

import exercise_pb2
import exercise_pb2_grpc
from app.grpc.session_registry import SessionRegistry
from app.services.pose_analysis_engine import PoseAnalysisEngine

logger = logging.getLogger(__name__)


class ExerciseServicer(exercise_pb2_grpc.ExerciseServiceServicer):
    """Inbound gRPC API exposed to Spring."""

    def __init__(self, engine: PoseAnalysisEngine, registry: SessionRegistry):
        self._engine = engine
        self._registry = registry

    def ExtractReferenceData(self, request, context):
        logger.info(
            "ExtractReferenceData requested for exercise_id=%s",
            request.exercise_id,
        )

        if request.extracted_poses:
            extracted_poses = list(request.extracted_poses)
        elif request.youtube_url:
            extracted_poses = []
        else:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "youtube_url is required")

        return exercise_pb2.ExtractResponse(
            success=True,
            exercise_id=request.exercise_id,
            extracted_poses=extracted_poses,
        )

    def StartAnalysis(self, request, context):
        logger.info("StartAnalysis requested for session_id=%s", request.session_id)

        if request.session_id <= 0:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "session_id must be positive")

        try:
            self._engine.start_analysis(
                session_id=request.session_id,
                exercise_id=request.exercise_id,
                reference_source=request.reference_source,
                reference_poses=list(request.reference_poses),
            )
        except ValueError as exc:
            context.abort(grpc.StatusCode.ALREADY_EXISTS, str(exc))

        now = Timestamp()
        now.GetCurrentTime()
        return exercise_pb2.AnalyzeResponse(
            success=True,
            session_id=request.session_id,
            exercise_id=request.exercise_id,
            start_time=now,
            status=exercise_pb2.SessionStatus.IN_PROGRESS,
        )

    def StopAnalysis(self, request, context):
        logger.info("StopAnalysis requested for session_id=%s", request.session_id)
        session = self._registry.stop(request.session_id)
        if session is None:
            context.abort(grpc.StatusCode.NOT_FOUND, "session not found")

        return exercise_pb2.StopResponse(
            success=True,
            message="Analysis stop signal accepted",
            session_id=request.session_id,
        )

    def GetFinalPoseData(self, request, context):
        session = self._registry.get(request.session_id)
        if session is None:
            context.abort(grpc.StatusCode.NOT_FOUND, "session not found")

        return exercise_pb2.PoseDataList(pose_data=session.pose_history)
