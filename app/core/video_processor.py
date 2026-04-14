"""Video processing helpers for uploaded exercise clips."""

import os
import tempfile

import cv2

from app.config import settings
from app.core.angle_calculator import extract_angles
from app.core.mediapipe_detector import get_detector
from app.core.pose_filter import NormalizedRoi, PoseTargetTracker
from app.core.squat_analyzer import analyze_squat_frames
from app.models.pose import Landmark
from app.models.video import FrameResult, VideoAnalysisResult


def analyze_video(video_path: str, exercise_type: str) -> VideoAnalysisResult:
    """Analyze a local video file and extract frame-level pose data."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise FileNotFoundError(f"Video could not be opened: {video_path}")

    original_fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / original_fps if original_fps > 0 else 0

    frame_interval = max(1, int(original_fps / settings.VIDEO_PROCESS_FPS))
    processed_fps = original_fps / frame_interval if original_fps > 0 else settings.VIDEO_PROCESS_FPS

    detector = get_detector()
    pose_tracker = PoseTargetTracker(
        roi=NormalizedRoi(
            min_x=settings.SQUAT_ROI_MIN_X,
            min_y=settings.SQUAT_ROI_MIN_Y,
            max_x=settings.SQUAT_ROI_MAX_X,
            max_y=settings.SQUAT_ROI_MAX_Y,
        )
    )
    frames: list[FrameResult] = []
    landmark_frames: list[list[Landmark] | None] = []
    frame_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % frame_interval == 0:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            landmarks = pose_tracker.filter(detector.detect(rgb))
            landmark_frames.append(landmarks)

            if landmarks:
                angles = extract_angles(landmarks, exercise_type)
                timestamp = frame_idx / original_fps if original_fps > 0 else 0.0
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

    squat_summary = None
    if exercise_type == "squat":
        min_rep_frames = max(4, int(processed_fps * 1.2))
        squat_frame_metrics, squat_summary = analyze_squat_frames(
            landmark_frames,
            min_rep_frames=min_rep_frames,
        )
        valid_metric_index = 0
        for frame in frames:
            while (
                valid_metric_index < len(landmark_frames)
                and landmark_frames[valid_metric_index] is None
            ):
                valid_metric_index += 1
            if valid_metric_index < len(squat_frame_metrics):
                frame.squat_metrics = squat_frame_metrics[valid_metric_index]
                valid_metric_index += 1

    return VideoAnalysisResult(
        exercise_type=exercise_type,
        total_frames=total_frames,
        analyzed_frames=len(frames),
        fps=original_fps,
        duration=round(duration, 2),
        frames=frames,
        squat_analysis=squat_summary,
    )


def analyze_video_bytes(video_bytes: bytes, exercise_type: str) -> VideoAnalysisResult:
    """Analyze uploaded video bytes by writing them to a temporary file."""
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(video_bytes)
        tmp_path = tmp.name

    try:
        return analyze_video(tmp_path, exercise_type)
    finally:
        os.unlink(tmp_path)
