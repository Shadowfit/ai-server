"""Run a live squat demo with pose overlay and basic analysis."""

from __future__ import annotations

import argparse
from collections import deque

import cv2

from app.core.squat_analyzer import analyze_squat_frames
from app.core.mediapipe_detector import get_detector
from app.models.pose import Landmark
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


def _pixel_point(landmarks: list[Landmark], name: str, width: int, height: int) -> tuple[int, int]:
    landmark = landmarks[LANDMARK[name]]
    return int(landmark.x * width), int(landmark.y * height)


def _draw_pose_overlay(frame, landmarks: list[Landmark]) -> None:
    height, width = frame.shape[:2]

    for start_name, end_name in POSE_CONNECTIONS:
        start_point = _pixel_point(landmarks, start_name, width, height)
        end_point = _pixel_point(landmarks, end_name, width, height)
        cv2.line(frame, start_point, end_point, (80, 255, 160), 3)

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
        point = _pixel_point(landmarks, name, width, height)
        cv2.circle(frame, point, 6, (0, 120, 255), -1)


def _status_color(phase: str) -> tuple[int, int, int]:
    if phase == "bottom":
        return (0, 255, 0)
    if phase in {"descending", "ascending"}:
        return (0, 200, 255)
    if phase == "standing":
        return (255, 255, 255)
    return (160, 160, 160)


def _fit_text(
    frame,
    text: str,
    origin: tuple[int, int],
    scale: float,
    color: tuple[int, int, int],
    thickness: int,
    max_width: int,
) -> None:
    """Draw text and shrink it when needed to fit the info panel."""
    current_scale = scale
    current_thickness = thickness
    while current_scale > 0.35:
        (text_width, _), _ = cv2.getTextSize(
            text, cv2.FONT_HERSHEY_SIMPLEX, current_scale, current_thickness
        )
        if text_width <= max_width:
            break
        current_scale -= 0.05
        current_thickness = max(1, current_thickness - 1)

    cv2.putText(
        frame,
        text,
        origin,
        cv2.FONT_HERSHEY_SIMPLEX,
        current_scale,
        color,
        current_thickness,
        cv2.LINE_AA,
    )


def _overlay_text(frame, rep_count: int, knee_angle: float | None, phase: str, feedback: str) -> None:
    height, width = frame.shape[:2]
    color = _status_color(phase)
    rows = [
        f"Reps: {rep_count}",
        f"Knee angle: {knee_angle:.1f} deg" if knee_angle is not None else "Knee angle: --",
        f"Phase: {phase}",
        f"Feedback: {feedback}",
        "ESC: quit",
    ]

    margin = max(12, int(min(width, height) * 0.02))
    panel_width = min(max(250, int(width * 0.34)), width - margin * 2)
    panel_height = min(max(110, int(height * 0.24)), height - margin * 2)
    top_left = (margin, margin)
    bottom_right = (margin + panel_width, margin + panel_height)

    title_scale = max(0.45, min(width, height) / 1200)
    body_scale = max(0.38, min(width, height) / 1450)
    body_thickness = 1 if min(width, height) < 900 else 2

    cv2.rectangle(frame, top_left, bottom_right, (25, 25, 25), -1)
    cv2.rectangle(frame, top_left, bottom_right, color, 2)

    for index, text in enumerate(rows):
        row_y = margin + 24 + int((index + 1) * (panel_height - 22) / (len(rows) + 1))
        _fit_text(
            frame,
            text,
            (margin + 16, row_y),
            title_scale if index == 0 else body_scale,
            color if index < 3 else (230, 230, 230),
            body_thickness,
            panel_width - 32,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a live squat demo with overlay.")
    parser.add_argument("--camera-index", type=int, default=0, help="OpenCV camera index")
    parser.add_argument(
        "--history-size",
        type=int,
        default=30,
        help="Number of recent analyzed frames used for squat state tracking",
    )
    args = parser.parse_args()

    detector = get_detector()
    frame_history: deque[list[Landmark] | None] = deque(maxlen=args.history_size)
    total_rep_count = 0
    seen_bottom_live = False
    previous_knee_angle: float | None = None

    cap = cv2.VideoCapture(args.camera_index)
    if not cap.isOpened():
        raise RuntimeError("Could not open the webcam.")

    window_name = "ShadowFit Live Demo"
    frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1280)
    frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 720)
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window_name, frame_width, frame_height)
    print("Live squat demo started. Press ESC to quit.")

    while True:
        ok, frame = cap.read()
        if not ok:
            break

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        landmarks = detector.detect(rgb)
        frame_history.append(landmarks)

        rep_count = total_rep_count
        knee_angle = None
        phase = "no_pose"
        feedback = "Stand fully in frame"

        if landmarks:
            _draw_pose_overlay(frame, landmarks)
            frame_metrics, summary = analyze_squat_frames(list(frame_history))

            latest_metric = next(
                (metric for metric in reversed(frame_metrics) if metric is not None),
                None,
            )
            if latest_metric is not None:
                knee_angle = latest_metric.knee_angle
                phase = latest_metric.phase
                if knee_angle <= 100:
                    seen_bottom_live = True
                if (
                    seen_bottom_live
                    and previous_knee_angle is not None
                    and previous_knee_angle < 140 <= knee_angle
                ):
                    total_rep_count += 1
                    seen_bottom_live = False
                previous_knee_angle = knee_angle
                rep_count = total_rep_count
            if summary.feedback:
                feedback = summary.feedback[0]
        else:
            previous_knee_angle = None
            cv2.putText(
                frame,
                "No pose detected",
                (40, frame.shape[0] - 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.9,
                (0, 0, 255),
                2,
                cv2.LINE_AA,
            )

        _overlay_text(frame, rep_count, knee_angle, phase, feedback)
        cv2.imshow(window_name, frame)

        if cv2.waitKey(1) & 0xFF == 27:
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
