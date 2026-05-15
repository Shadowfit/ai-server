"""실시간 포즈 감지 API.

session_id가 함께 오면 누적 분석 + rep 감지를 수행하고,
rep 1회가 완성될 때마다 Spring에 PoseData 묶음을 콜백한다.
"""

import json
import logging

import cv2

from fastapi import APIRouter

import exercise_pb2
from app.core.angle_calculator import extract_angles
from app.core.mediapipe_detector import get_detector
from app.core.squat_analyzer import StreamingSquatAnalyzer
from app.grpc import spring_client
from app.grpc.session_state import PerRepFrame, get_registry
from app.models.pose import Landmark, PoseRequest, PoseResponse
from app.utils.image_utils import base64_to_image

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pose", tags=["포즈 감지"])

# 운동 유형별 분석기 — stateless 클래스라 공유 가능
_analyzers: dict[str, StreamingSquatAnalyzer] = {
    "squat": StreamingSquatAnalyzer("squat"),
}


def _landmarks_to_json(landmarks: list[Landmark]) -> str:
    return json.dumps(
        [
            {"index": lm.index, "x": lm.x, "y": lm.y, "z": lm.z, "visibility": lm.visibility}
            for lm in landmarks
        ]
    )


@router.post("", response_model=PoseResponse)
async def detect_pose(req: PoseRequest):
    """Base64 이미지 → 관절 감지 + (선택) 세션 누적 분석."""
    image_bgr = base64_to_image(req.image)
    image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)

    detector = get_detector()
    landmarks = detector.detect(image_rgb)

    if not landmarks:
        return PoseResponse(success=False, message="포즈를 감지할 수 없습니다")

    # 세션 미지정 — 기존 stateless 동작 (각도만 반환)
    if req.session_id is None:
        angles = extract_angles(landmarks, req.exercise_type)
        return PoseResponse(success=True, landmarks=landmarks, angles=angles)

    state = get_registry().get(req.session_id)
    if state is None:
        return PoseResponse(
            success=False,
            message=f"세션 {req.session_id}가 시작되지 않았습니다 (StartAnalysis 먼저 호출 필요)",
        )

    analyzer = _analyzers.get(state.exercise_type)
    if analyzer is None:
        return PoseResponse(
            success=False, message=f"미지원 운동: {state.exercise_type}"
        )

    angles, rep_event = analyzer.process_frame(state, landmarks)

    if angles is None:
        # visibility 부족 — 프레임 스킵
        return PoseResponse(
            success=True,
            landmarks=landmarks,
            message="가시성 부족으로 분석 스킵",
            rep_count=state.rep_count,
        )

    timestamp_sec = (
        req.timestamp_sec if req.timestamp_sec is not None else float(state.frame_index)
    )
    frame = PerRepFrame(
        timestamp_sec=timestamp_sec,
        joint_coordinates=_landmarks_to_json(landmarks),
        angles=angles,
    )
    state.current_rep_frames.append(frame)

    if rep_event is None:
        return PoseResponse(
            success=True,
            landmarks=landmarks,
            angles=angles,
            rep_count=state.rep_count,
        )

    # rep 1회 완성 → Spring에 그 rep의 PoseData 묶음 콜백
    pose_data_list = [
        exercise_pb2.PoseDataRequest(
            timestamp_sec=f.timestamp_sec,
            joint_coordinates=f.joint_coordinates,
            sync_rate=rep_event.sync_rate,
            feedback_message=rep_event.feedback_message,
        )
        for f in state.current_rep_frames
    ]
    spring_client.report_pose_data_batch(state.session_id, pose_data_list)

    # 누적 요약 보관 + 현재 rep 버퍼 비우기
    from app.grpc.session_state import CompletedRep

    state.completed_reps.append(
        CompletedRep(
            rep_number=rep_event.rep_number,
            sync_rate=rep_event.sync_rate,
            frames=list(state.current_rep_frames),
            feedback_message=rep_event.feedback_message,
        )
    )
    state.current_rep_frames.clear()

    logger.info(
        "세션 %s rep %d 완성 (sync_rate=%.2f)",
        state.session_id,
        rep_event.rep_number,
        rep_event.sync_rate,
    )

    return PoseResponse(
        success=True,
        landmarks=landmarks,
        angles=angles,
        rep_count=state.rep_count,
        rep_completed=True,
        sync_rate=rep_event.sync_rate,
    )