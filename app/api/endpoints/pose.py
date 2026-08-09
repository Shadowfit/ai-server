"""실시간 포즈 감지 API.

session_id가 함께 오면 누적 분석 + rep 감지를 수행하고,
rep 1회가 완성될 때마다 Spring에 PoseData 묶음을 콜백한다.
"""

import json
import logging

import cv2

from fastapi import APIRouter

import exercise_pb2
from app.core.analyzer_registry import get_analyzer
from app.core.angle_calculator import extract_angles
from app.core.mediapipe_detector import get_detector
from app.grpc import spring_client
from app.grpc.session_state import PerRepFrame, get_registry
from app.models.pose import Landmark, PoseRequest, PoseResponse
from app.utils.image_utils import base64_to_image

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pose", tags=["포즈 감지"])

# 분석기 레지스트리는 app.core.analyzer_registry 로 옮겼다(이슈 #147). gRPC servicer 도 같은
# 표를 봐야 하는데, 그게 HTTP 엔드포인트 모듈에 있으면 참조 방향이 거꾸로가 된다.


def _landmarks_to_json(landmarks: list[Landmark]) -> str:
    return json.dumps(
        [
            {"index": lm.index, "x": lm.x, "y": lm.y, "z": lm.z, "visibility": lm.visibility}
            for lm in landmarks
        ]
    )


@router.post("", response_model=PoseResponse)
def detect_pose(req: PoseRequest):
    """Base64 이미지 → 관절 감지 + (선택) 세션 누적 분석.

    MediaPipe 추론, OpenCV 변환, Spring 콜백 gRPC가 모두 동기 블로킹이라
    `async def`로 두면 이벤트 루프를 점유해 다른 요청을 굶긴다. FastAPI는
    `def` 핸들러를 자동으로 threadpool에서 실행하므로 그대로 두면 된다.
    """
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

    analyzer = get_analyzer(state.exercise_type)
    if analyzer is None:
        return PoseResponse(
            success=False, message=f"미지원 운동: {state.exercise_type}"
        )

    angles, smoothed_knee_angle, rep_event = analyzer.process_frame(state, landmarks)

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
        smoothed_knee_angle=smoothed_knee_angle,
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
            # 재부착 시 Spring 이 MAX(rep_number) 로 rep 카운트를 복원하는 근거 (이슈 #59 2단계)
            rep_number=rep_event.rep_number,
            # sync_rate 는 rep 상수라 프레임을 구분하지 못한다. Spring 이 다운샘플에서 어느
            # 프레임을 남길지, 리포트에서 어느 프레임을 대표로 쓸지 고르는 기준이 이 값이다
            # (decisions/worst-section-rep-resolution.md §4-ㄹ). 작을수록 깊게 앉은 것.
            smoothed_knee_angle=f.smoothed_knee_angle,
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