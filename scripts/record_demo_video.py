"""Record a guided squat demo clip from the default webcam."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import cv2


GUIDE_LINES = [
    "Stand side-on to the camera",
    "Keep your full body inside the box",
    "Do 2 to 3 slow squat reps",
]


def draw_guides(frame, seconds_left: int) -> None:
    height, width = frame.shape[:2]
    cv2.rectangle(
        frame,
        (int(width * 0.18), int(height * 0.08)),
        (int(width * 0.82), int(height * 0.95)),
        (255, 255, 255),
        2,
    )
    cv2.putText(
        frame,
        f"Recording: {seconds_left:02d}s left",
        (30, 40),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (0, 255, 255),
        2,
        cv2.LINE_AA,
    )
    for idx, text in enumerate(GUIDE_LINES):
        cv2.putText(
            frame,
            text,
            (30, height - 110 + idx * 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Record a webcam squat demo clip.")
    parser.add_argument(
        "--output",
        default="demo_videos/demo_squat.mp4",
        help="Output video path",
    )
    parser.add_argument(
        "--seconds",
        type=int,
        default=10,
        help="Recording duration in seconds",
    )
    parser.add_argument(
        "--camera-index",
        type=int,
        default=0,
        help="OpenCV camera index",
    )
    args = parser.parse_args()

    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(args.camera_index)
    if not cap.isOpened():
        raise RuntimeError("Could not open the webcam.")

    fps = cap.get(cv2.CAP_PROP_FPS)
    fps = 20.0 if fps <= 1 else min(fps, 30.0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1280)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 720)

    writer = cv2.VideoWriter(
        str(output_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )

    print("Prepare for recording. Stand side-on to the camera.")
    window_name = "ShadowFit Demo Recorder"
    for countdown in range(3, 0, -1):
        ok, frame = cap.read()
        if not ok:
            break
        cv2.putText(
            frame,
            f"Starting in {countdown}",
            (40, 80),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.4,
            (0, 255, 0),
            3,
            cv2.LINE_AA,
        )
        draw_guides(frame, args.seconds)
        writer.write(frame)
        print(f"Starting in {countdown}...")
        cv2.imshow(window_name, frame)
        if cv2.waitKey(1000) & 0xFF == 27:
            cap.release()
            writer.release()
            cv2.destroyAllWindows()
            print("Recording cancelled.")
            return

    start = time.time()
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        elapsed = time.time() - start
        seconds_left = max(0, args.seconds - int(elapsed))
        draw_guides(frame, seconds_left)
        writer.write(frame)
        cv2.imshow(window_name, frame)
        if cv2.waitKey(1) & 0xFF == 27:
            print("Recording stopped early by user.")
            break
        if elapsed >= args.seconds:
            break

    cap.release()
    writer.release()
    cv2.destroyAllWindows()
    print(f"Saved demo video to: {output_path}")


if __name__ == "__main__":
    main()
