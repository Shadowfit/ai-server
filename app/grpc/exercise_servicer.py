"""ExerciseService gRPC servicer.

Spring → FastAPI 진입점:
- StartAnalysis: reference 좌표를 받아 세션 상태 초기화
- StopAnalysis: 누적 결과로 CompleteAnalysis 콜백
- ExtractReferenceData: YouTube 좌표 추출 (현재는 빈 응답 — 별도 작업으로 분리)
"""

from __future__ import annotations

import json
import logging
import threading
import time

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

import exercise_pb2
import exercise_pb2_grpc
from app.core.angle_calculator import extract_angles
from app.grpc import spring_client
from app.grpc.session_state import get_registry
from app.models.pose import Landmark

logger = logging.getLogger(__name__)


def _parse_reference_poses(
    reference_poses, exercise_type: str
) -> list[list[float]]:
    """Spring이 보낸 reference PoseDataRequest 리스트 → 각도 시퀀스로 변환."""
    sequences: list[list[float]] = []
    for ref in reference_poses:
        if not ref.joint_coordinates:
            continue
        try:
            raw = json.loads(ref.joint_coordinates)
            landmarks = [
                Landmark(
                    index=item["index"],
                    x=item["x"],
                    y=item["y"],
                    z=item.get("z", 0.0),
                    visibility=item.get("visibility", 1.0),
                )
                for item in raw
            ]
            sequences.append(extract_angles(landmarks, exercise_type))
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("reference 좌표 파싱 실패: %s", e)
            continue
    return sequences


class ExerciseServicer(exercise_pb2_grpc.ExerciseServiceServicer):

    def StartAnalysis(self, request, context):
        """[Spring → FastAPI] 운동 분석 세션 시작."""
        session_id = request.session_id
        exercise_id = request.exercise_id
        logger.info(
            "[Spring → AI] StartAnalysis 수신 (session=%s, exercise=%s, reference_frames=%d)",
            session_id,
            exercise_id,
            len(request.reference_poses),
        )

        # 일단 squat으로 가정. 추후 exercise_id → exercise_type 매핑이 필요.
        exercise_type = "squat"
        reference_angles = _parse_reference_poses(
            request.reference_poses, exercise_type
        )

        if not reference_angles:
            logger.warning(
                "세션 %s에 reference 각도 시퀀스가 비어 있음 — sync_rate는 0으로 계산됨",
                session_id,
            )

        get_registry().create(
            session_id=session_id,
            exercise_id=exercise_id,
            reference_angles=reference_angles,
            exercise_type=exercise_type,
        )

        now = Timestamp()
        now.GetCurrentTime()
        return exercise_pb2.AnalyzeResponse(
            success=True,
            session_id=session_id,
            exercise_id=exercise_id,
            start_time=now,
            status=exercise_pb2.SessionStatus.IN_PROGRESS,
        )

    def StopAnalysis(self, request, context):
        """[Spring → FastAPI] 사용자 강제 중단. 누적 결과로 CompleteAnalysis 콜백."""
        session_id = request.session_id
        logger.info("[Spring → AI] StopAnalysis 수신 (session=%s)", session_id)

        state = get_registry().remove(session_id)
        if state is None:
            return exercise_pb2.StopResponse(
                success=False,
                message="진행 중인 세션을 찾을 수 없습니다.",
                session_id=session_id,
            )

        # 누적된 rep들로 최종 통계 산출 → 별도 스레드에서 Spring 콜백
        threading.Thread(
            target=_send_complete_analysis,
            args=(state,),
            daemon=True,
        ).start()

        return exercise_pb2.StopResponse(
            success=True,
            message="분석 중단 및 결과 보고 예약 완료.",
            session_id=session_id,
        )

    def ExtractReferenceData(self, request, context):
        """[Spring → FastAPI] YouTube URL → 기준 좌표 추출.

        실제 YouTube 다운로드/MediaPipe 추출은 별도 작업으로 분리.
        현재는 빈 응답을 돌려주어 인터페이스 호환만 유지한다.
        """
        logger.info(
            "[Spring → AI] ExtractReferenceData 수신 (exercise=%s, url=%s) — 미구현",
            request.exercise_id,
            request.youtube_url,
        )
        return exercise_pb2.ExtractResponse(
            success=True,
            exercise_id=request.exercise_id,
            extracted_poses=[],
        )


def _send_complete_analysis(state) -> None:
    """완료된 rep들의 통계를 모아 Spring에 CompleteAnalysis 호출."""
    # gRPC 서버 쓰레드와 다른 컨텍스트라서 작은 지연으로 race 회피
    time.sleep(0.1)

    reps = state.completed_reps
    total_reps = len(reps)
    if total_reps == 0:
        avg = max_v = min_v = 0.0
    else:
        rates = [r.sync_rate for r in reps]
        avg = round(sum(rates) / total_reps, 2)
        max_v = round(max(rates), 2)
        min_v = round(min(rates), 2)

    spring_client.report_complete_analysis(
        session_id=state.session_id,
        total_reps=total_reps,
        avg_sync_rate=avg,
        max_sync_rate=max_v,
        min_sync_rate=min_v,
        calories_burned=0.0,  # 칼로리 계산은 추후
    )