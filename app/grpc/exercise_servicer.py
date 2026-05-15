from __future__ import annotations

import logging

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

import exercise_pb2
import exercise_pb2_grpc

logger = logging.getLogger(__name__)


class ExerciseServicer(exercise_pb2_grpc.ExerciseServiceServicer):

        logger.info(
        )

        )


        )

        now = Timestamp()
        now.GetCurrentTime()
        return exercise_pb2.AnalyzeResponse(
            success=True,
            start_time=now,
            status=exercise_pb2.SessionStatus.IN_PROGRESS,
        )

    def StopAnalysis(self, request, context):

            return exercise_pb2.StopResponse(
            success=True,
        )


