from __future__ import annotations

import json
import os
from statistics import mean
from time import sleep

import exercise_pb2
import numpy as np

from app.config import settings
from app.core.dtw_calculator import compute_sync_rate
from app.core.video_processor import analyze_video
from app.grpc.session_registry import AnalysisSession, SessionRegistry
from app.grpc.spring_client import SpringGrpcClient


class PoseAnalysisEngine:
    """Drive a session, compare angle sequences, and relay results to Spring."""

    def __init__(self, registry: SessionRegistry, spring_client: SpringGrpcClient):
        self._registry = registry
        self._spring_client = spring_client

    def start_analysis(
        self,
        session_id: int,
        exercise_id: int,
        reference_source: str,
        reference_poses: list[exercise_pb2.PoseDataRequest],
    ) -> AnalysisSession:
        from threading import Thread

        session = self._registry.create(session_id, exercise_id, reference_source)
        thread = Thread(
            target=self.run_analysis,
            args=(session, reference_poses),
            daemon=True,
            name=f"analysis-session-{session_id}",
        )
        self._registry.set_thread(session_id, thread)
        thread.start()
        return session

    def run_analysis(
        self,
        session: AnalysisSession,
        reference_poses: list[exercise_pb2.PoseDataRequest],
    ) -> None:
        reference_sequence = self._extract_reference_sequence(reference_poses)
        user_frames = self._load_user_frames(session.reference_source)

        if not user_frames and reference_sequence:
            # Fallback mode so Spring integration can still exercise the flow
            # before a real user-video source is fully wired in.
            user_frames = [
                {"timestamp_sec": float(index), "angles": angles}
                for index, angles in enumerate(reference_sequence)
            ]

        pose_batch: list[exercise_pb2.PoseDataRequest] = []
        sync_rates: list[float] = []

        for index, user_frame in enumerate(user_frames):
            if session.stop_event.is_set():
                break

            user_window = self._build_user_window(user_frames, index)
            current_window = self._build_reference_window(
                reference_sequence,
                len(user_window),
            )
            sync_rate = (
                compute_sync_rate(current_window, user_window)
                if current_window and user_window
                else 0.0
            )
            sync_rates.append(sync_rate)

            payload = {
                "angles": user_frame["angles"],
                "sync_rate": sync_rate,
                "reference_source": session.reference_source,
            }
            pose_batch.append(
                exercise_pb2.PoseDataRequest(
                    timestamp_sec=user_frame["timestamp_sec"],
                    joint_coordinates=json.dumps(payload, separators=(",", ":")),
                )
            )

            if len(pose_batch) >= settings.POSE_BATCH_SIZE:
                self.stream_intermediate_data(session.session_id, pose_batch)
                pose_batch = []

            sleep(0.01)

        if pose_batch:
            self.stream_intermediate_data(session.session_id, pose_batch)

        status = (
            exercise_pb2.SessionStatus.FAILED
            if session.stop_event.is_set()
            else exercise_pb2.SessionStatus.COMPLETED
        )
        final_result = self._build_final_result(
            session.session_id,
            sync_rates,
            len(user_frames),
            status,
        )
        self.finalize_analysis(session.session_id, final_result, status)

    def stream_intermediate_data(
        self,
        session_id: int,
        pose_batch: list[exercise_pb2.PoseDataRequest],
    ) -> None:
        self._registry.append_pose_batch(session_id, pose_batch)
        self._spring_client.save_pose_data_batch(session_id, pose_batch)

    def finalize_analysis(
        self,
        session_id: int,
        final_result: exercise_pb2.SessionCompleteRequest,
        status: int,
    ) -> None:
        self._registry.mark_final(session_id, final_result, status)
        self._spring_client.complete_analysis(final_result)

    def _extract_reference_sequence(
        self,
        reference_poses: list[exercise_pb2.PoseDataRequest],
    ) -> list[list[float]]:
        sequence: list[list[float]] = []

        for pose in reference_poses:
            extracted = self._parse_joint_coordinates(pose.joint_coordinates)
            if extracted:
                sequence.extend(extracted)

        return sequence

    def _parse_joint_coordinates(self, raw_value: str) -> list[list[float]]:
        if not raw_value:
            return []

        parsed = self._safe_json_load(raw_value)
        if parsed is None:
            numeric_row = self._parse_numeric_row(raw_value)
            return [numeric_row] if numeric_row else []

        if isinstance(parsed, dict):
            if "representative_sequence" in parsed:
                return self._normalize_sequence(parsed["representative_sequence"])
            if "angles" in parsed:
                angles = self._coerce_angle_row(parsed["angles"])
                return [angles] if angles else []

        if isinstance(parsed, list):
            if parsed and isinstance(parsed[0], list):
                return self._normalize_sequence(parsed)
            angles = self._coerce_angle_row(parsed)
            return [angles] if angles else []

        return []

    def _safe_json_load(self, raw_value: str):
        try:
            return json.loads(raw_value)
        except json.JSONDecodeError:
            return None

    def _parse_numeric_row(self, raw_value: str) -> list[float]:
        parts = [part.strip() for part in raw_value.split(",")]
        try:
            return [float(part) for part in parts if part]
        except ValueError:
            return []

    def _normalize_sequence(self, values) -> list[list[float]]:
        sequence: list[list[float]] = []
        for row in values:
            angles = self._coerce_angle_row(row)
            if angles:
                sequence.append(angles)
        return sequence

    def _coerce_angle_row(self, row) -> list[float]:
        if not isinstance(row, list):
            return []
        try:
            return [float(value) for value in row]
        except (TypeError, ValueError):
            return []

    def _load_user_frames(self, source: str) -> list[dict[str, list[float] | float]]:
        if not source:
            return []

        if os.path.exists(source):
            if source.lower().endswith(".json"):
                return self._load_frames_from_json_file(source)
            return self._load_frames_from_video(source)

        parsed = self._safe_json_load(source)
        if parsed is not None:
            return self._frames_from_json_payload(parsed)

        return []

    def _load_frames_from_video(self, video_path: str) -> list[dict[str, list[float] | float]]:
        result = analyze_video(video_path, "squat")
        frames: list[dict[str, list[float] | float]] = []
        for frame in result.frames:
            frames.append(
                {
                    "timestamp_sec": frame.timestamp,
                    "angles": [float(value) for value in frame.angles],
                }
            )
        return frames

    def _load_frames_from_json_file(self, path: str) -> list[dict[str, list[float] | float]]:
        with open(path, "r", encoding="utf-8") as file:
            payload = json.load(file)
        return self._frames_from_json_payload(payload)

    def _frames_from_json_payload(self, payload) -> list[dict[str, list[float] | float]]:
        if isinstance(payload, dict) and "representative_sequence" in payload:
            sequence = self._normalize_sequence(payload["representative_sequence"])
            return [
                {"timestamp_sec": float(index), "angles": angles}
                for index, angles in enumerate(sequence)
            ]

        if isinstance(payload, dict) and "frames" in payload:
            frames: list[dict[str, list[float] | float]] = []
            for frame in payload["frames"]:
                if not isinstance(frame, dict):
                    continue
                angles = self._coerce_angle_row(frame.get("angles"))
                if angles:
                    frames.append(
                        {
                            "timestamp_sec": float(frame.get("timestamp", frame.get("timestamp_sec", len(frames)))),
                            "angles": angles,
                        }
                    )
            return frames

        if isinstance(payload, list):
            sequence = self._normalize_sequence(payload)
            return [
                {"timestamp_sec": float(index), "angles": angles}
                for index, angles in enumerate(sequence)
            ]

        return []

    def _build_reference_window(
        self,
        reference_sequence: list[list[float]],
        target_length: int,
    ) -> list[list[float]]:
        if not reference_sequence or target_length <= 0:
            return []
        if len(reference_sequence) == target_length:
            return reference_sequence

        ref_array = np.array(reference_sequence, dtype=np.float64)
        source_axis = np.linspace(0.0, 1.0, num=len(reference_sequence))
        target_axis = np.linspace(0.0, 1.0, num=target_length)
        resampled_columns = []
        for angle_index in range(ref_array.shape[1]):
            resampled_columns.append(
                np.interp(target_axis, source_axis, ref_array[:, angle_index])
            )

        normalized: list[list[float]] = []
        for frame_index in range(target_length):
            normalized.append(
                [round(float(column[frame_index]), 4) for column in resampled_columns]
            )
        return normalized

    def _build_user_window(
        self,
        user_frames: list[dict[str, list[float] | float]],
        frame_index: int,
    ) -> list[list[float]]:
        start_index = max(0, frame_index - settings.POSE_BATCH_SIZE + 1)
        window = user_frames[start_index : frame_index + 1]
        return [frame["angles"] for frame in window]

    def _build_final_result(
        self,
        session_id: int,
        sync_rates: list[float],
        frame_count: int,
        status: int,
    ) -> exercise_pb2.SessionCompleteRequest:
        avg_sync_rate = round(mean(sync_rates), 2) if sync_rates else 0.0
        max_sync_rate = max(sync_rates) if sync_rates else 0.0
        min_sync_rate = min(sync_rates) if sync_rates else 0.0
        total_reps = max(0, frame_count // 30)
        calories_burned = round(total_reps * 0.5, 2)

        if status == exercise_pb2.SessionStatus.FAILED:
            calories_burned = 0.0

        return exercise_pb2.SessionCompleteRequest(
            session_id=session_id,
            total_reps=total_reps,
            avg_sync_rate=avg_sync_rate,
            max_sync_rate=max_sync_rate,
            min_sync_rate=min_sync_rate,
            calories_burned=calories_burned,
            difficulty_level=1,
        )
