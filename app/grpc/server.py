from __future__ import annotations

import logging
from concurrent import futures

import grpc
import exercise_pb2_grpc
from app.grpc.exercise_servicer import ExerciseServicer




    global _server
        if _server is not None: