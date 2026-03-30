"""영상 전처리 모듈 — 참고 영상에서 관절 좌표를 미리 추출."""

import cv2
import numpy as np

from app.config import settings
from app.core.angle_calculator import extract_angles
from app.core.mediapipe_detector import get_detector
from app.models.pose import Landmark
from app.models.video import FrameResult, VideoAnalysisResult


def analyze_video(
    video_path: str, exercise_type: str
) -> VideoAnalysisResult:
    """영상 파일을 분석하여 프레임별 관절 각도를 추출한다.

    Args:
        video_path: 영상 파일 경로
        exercise_type: 운동 유형 (squat, deadlift, pullup)

    Returns:
        프레임별 분석 결과
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise FileNotFoundError(f"영상을 열 수 없음: {video_path}")

    original_fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / original_fps if original_fps > 0 else 0

    # 분석할 프레임 간격 계산
    frame_interval = max(1, int(original_fps / settings.VIDEO_PROCESS_FPS))

    detector = get_detector()
    frames: list[FrameResult] = []
    frame_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % frame_interval == 0:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            landmarks = detector.detect(rgb)

            if landmarks:
                angles = extract_angles(landmarks, exercise_type)
                timestamp = frame_idx / original_fps
                frames.append(
                    FrameResult(
                        frame_index=frame_idx,
                        timestamp=round(timestamp, 3),
                        landmarks=landmarks,
                        angles=angles,
                    )
                )
        frame_idx += 1

    cap.release()

    return VideoAnalysisResult(
        exercise_type=exercise_type,
        total_frames=total_frames,
        analyzed_frames=len(frames),
        fps=original_fps,
        duration=round(duration, 2),
        frames=frames,
    )


def analyze_video_bytes(
    video_bytes: bytes, exercise_type: str
) -> VideoAnalysisResult:
    """바이트 데이터로 영상을 분석한다 (업로드된 파일용)."""
    import tempfile, os

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(video_bytes)
        tmp_path = tmp.name

    try:
        return analyze_video(tmp_path, exercise_type)
    finally:
        os.unlink(tmp_path)
