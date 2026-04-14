"""Render a squat video with pose, angle, phase, and rep-count overlays."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2

from app.core.video_processor import analyze_video
from app.core.pose_filter import DEFAULT_SQUAT_ROI, NormalizedRoi
from app.models.pose import Landmark
from app.models.video import FrameResult, VideoAnalysisResult
from app.utils.constants import LANDMARK


POSE_CONNECTIONS = [
    ("LEFT_SHOULDER", "RIGHT_SHOULDER"),
    ("LEFT_SHOULDER", "LEFT_HIP"),
    ("RIGHT_SHOULDER", "RIGHT_HIP"),
    ("LEFT_HIP", "RIGHT_HIP"),
    ("LEFT_HIP", "LEFT_KNEE"),
    ("RIGHT_HIP", "RIGHT_KNEE"),
    ("LEFT_KNEE", "LEFT_ANKLE"),
    ("RIGHT_KNEE", "RIGHT_ANKLE"),
]


def _pixel_point(
    landmarks: list[Landmark], name: str, width: int, height: int
) -> tuple[int, int]:
    landmark = landmarks[LANDMARK[name]]
    return int(landmark.x * width), int(landmark.y * height)


def _draw_pose(frame, landmarks: list[Landmark]) -> None:
    height, width = frame.shape[:2]
    for start_name, end_name in POSE_CONNECTIONS:
        cv2.line(
            frame,
            _pixel_point(landmarks, start_name, width, height),
            _pixel_point(landmarks, end_name, width, height),
            (80, 255, 160),
            3,
        )

    for name in {
        "LEFT_SHOULDER",
        "RIGHT_SHOULDER",
        "LEFT_HIP",
        "RIGHT_HIP",
        "LEFT_KNEE",
        "RIGHT_KNEE",
        "LEFT_ANKLE",
        "RIGHT_ANKLE",
    }:
        cv2.circle(frame, _pixel_point(landmarks, name, width, height), 6, (0, 120, 255), -1)


def _status_color(phase: str) -> tuple[int, int, int]:
    if phase == "bottom":
        return (0, 255, 0)
    if phase in {"descending", "ascending"}:
        return (0, 200, 255)
    if phase == "standing":
        return (255, 255, 255)
    return (170, 170, 170)


def _put_text(
    frame,
    text: str,
    origin: tuple[int, int],
    scale: float,
    color: tuple[int, int, int],
    thickness: int = 2,
) -> None:
    cv2.putText(
        frame,
        text,
        origin,
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        color,
        thickness,
        cv2.LINE_AA,
    )


def _draw_angle_label(
    frame,
    landmarks: list[Landmark],
    label: str,
    landmark_name: str,
    angle: float,
    offset_x: int,
) -> None:
    height, width = frame.shape[:2]
    knee_x, knee_y = _pixel_point(landmarks, landmark_name, width, height)
    _put_text(
        frame,
        f"{label}: {angle:.1f}",
        (knee_x + offset_x, knee_y - 12),
        0.58,
        (255, 255, 0),
        2,
    )


def _draw_panel(frame, frame_result: FrameResult | None, is_count_event: bool) -> None:
    height, width = frame.shape[:2]
    margin = max(14, int(min(width, height) * 0.018))
    panel_width = min(max(360, int(width * 0.36)), width - margin * 2)
    panel_height = min(max(170, int(height * 0.26)), height - margin * 2)
    top_left = (margin, margin)
    bottom_right = (margin + panel_width, margin + panel_height)

    if frame_result is None or frame_result.squat_metrics is None:
        rows = ["Pose: not analyzed", "Reps: --", "Knee: --", "Phase: --"]
        color = (170, 170, 170)
    else:
        metrics = frame_result.squat_metrics
        color = _status_color(metrics.phase)
        rows = [
            f"Time: {frame_result.timestamp:.2f}s",
            f"Reps: {metrics.rep_count}",
            f"Knee avg: {metrics.knee_angle:.1f} deg",
            f"Hip avg: {metrics.hip_angle:.1f} deg",
            f"Torso tilt: {metrics.torso_tilt:.1f} deg",
            f"Phase: {metrics.phase}",
        ]

    cv2.rectangle(frame, top_left, bottom_right, (20, 20, 20), -1)
    cv2.rectangle(frame, top_left, bottom_right, color, 2)

    if is_count_event:
        cv2.rectangle(frame, (0, 0), (width - 1, height - 1), (0, 255, 255), 8)
        _put_text(frame, "REP COUNT +1", (margin, bottom_right[1] + 44), 1.0, (0, 255, 255), 3)

    line_step = max(24, int(panel_height / (len(rows) + 1)))
    for index, row in enumerate(rows):
        _put_text(
            frame,
            row,
            (margin + 16, margin + 30 + index * line_step),
            0.62,
            color if index < 2 else (235, 235, 235),
            2,
        )


def _draw_roi(frame, roi: NormalizedRoi) -> None:
    height, width = frame.shape[:2]
    top_left = (int(roi.min_x * width), int(roi.min_y * height))
    bottom_right = (int(roi.max_x * width), int(roi.max_y * height))
    cv2.rectangle(frame, top_left, bottom_right, (255, 180, 0), 3)
    _put_text(frame, "Squat ROI", (top_left[0] + 10, max(28, top_left[1] - 12)), 0.75, (255, 180, 0), 2)


def _load_or_create_analysis(video_path: Path, analysis_path: Path | None) -> VideoAnalysisResult:
    if analysis_path and analysis_path.exists():
        payload = json.loads(analysis_path.read_text(encoding="utf-8"))
        return VideoAnalysisResult.model_validate(payload)

    result = analyze_video(str(video_path), "squat")
    if analysis_path:
        analysis_path.parent.mkdir(parents=True, exist_ok=True)
        analysis_path.write_text(
            json.dumps(result.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return result


def render_annotated_video(
    video_path: Path,
    output_path: Path,
    analysis: VideoAnalysisResult,
    *,
    roi: NormalizedRoi | None = DEFAULT_SQUAT_ROI,
) -> None:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise FileNotFoundError(f"Video could not be opened: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or analysis.fps or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1280)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 720)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or analysis.total_frames)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(output_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )

    frames_by_index = {frame.frame_index: frame for frame in analysis.frames}
    count_event_frames: set[int] = set()
    previous_count = 0
    for frame in analysis.frames:
        metrics = frame.squat_metrics
        if metrics and metrics.rep_count > previous_count:
            count_event_frames.add(frame.frame_index)
            previous_count = metrics.rep_count

    latest_result: FrameResult | None = None
    latest_event_frame = -10_000
    frame_index = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break

        if frame_index in frames_by_index:
            latest_result = frames_by_index[frame_index]
            if frame_index in count_event_frames:
                latest_event_frame = frame_index

        is_count_event = 0 <= frame_index - latest_event_frame <= int(fps * 0.25)

        if roi is not None:
            _draw_roi(frame, roi)

        if latest_result is not None:
            _draw_pose(frame, latest_result.landmarks)
            if len(latest_result.angles) >= 2:
                _draw_angle_label(
                    frame,
                    latest_result.landmarks,
                    "L knee",
                    "LEFT_KNEE",
                    latest_result.angles[0],
                    -120,
                )
                _draw_angle_label(
                    frame,
                    latest_result.landmarks,
                    "R knee",
                    "RIGHT_KNEE",
                    latest_result.angles[1],
                    18,
                )

        _draw_panel(frame, latest_result, is_count_event)
        writer.write(frame)

        frame_index += 1
        if frame_index % 300 == 0:
            print(f"Rendered {frame_index}/{total_frames} frames...")

    cap.release()
    writer.release()
    print(f"Annotated video saved to: {output_path.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Render MediaPipe squat overlays to a video.")
    parser.add_argument("video", help="Path to the squat video file")
    parser.add_argument(
        "--analysis",
        help="Optional existing analysis JSON. If missing, the script creates it.",
    )
    parser.add_argument("--output", help="Output annotated MP4 path")
    parser.add_argument(
        "--roi",
        nargs=4,
        type=float,
        metavar=("MIN_X", "MIN_Y", "MAX_X", "MAX_Y"),
        default=(
            DEFAULT_SQUAT_ROI.min_x,
            DEFAULT_SQUAT_ROI.min_y,
            DEFAULT_SQUAT_ROI.max_x,
            DEFAULT_SQUAT_ROI.max_y,
        ),
        help="Normalized ROI box drawn on the output video.",
    )
    args = parser.parse_args()

    video_path = Path(args.video).resolve()
    analysis_path = Path(args.analysis).resolve() if args.analysis else None
    output_path = (
        Path(args.output).resolve()
        if args.output
        else video_path.with_name(f"{video_path.stem}_annotated.mp4")
    )

    analysis = _load_or_create_analysis(video_path, analysis_path)
    roi = NormalizedRoi(*args.roi) if args.roi else None
    render_annotated_video(video_path, output_path, analysis, roi=roi)


if __name__ == "__main__":
    main()
