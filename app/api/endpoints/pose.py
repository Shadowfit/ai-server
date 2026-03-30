"""실시간 포즈 감지 API."""

import cv2

from fastapi import APIRouter

from app.core.angle_calculator import extract_angles
from app.core.mediapipe_detector import get_detector
from app.models.pose import PoseRequest, PoseResponse
from app.utils.image_utils import base64_to_image

router = APIRouter(prefix="/pose", tags=["포즈 감지"])


@router.post("", response_model=PoseResponse)
async def detect_pose(req: PoseRequest):
    """Base64 이미지에서 관절을 감지하고 각도를 계산한다.

    React Native에서 카메라 프레임을 Base64로 전송하면
    MediaPipe로 관절 좌표를 추출하고 운동별 각도를 반환한다.
    """
    image_bgr = base64_to_image(req.image)
    image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)

    detector = get_detector()
    landmarks = detector.detect(image_rgb)

    if not landmarks:
        return PoseResponse(success=False, message="포즈를 감지할 수 없습니다")

    angles = extract_angles(landmarks, req.exercise_type)

    return PoseResponse(
        success=True,
        landmarks=landmarks,
        angles=angles,
    )
