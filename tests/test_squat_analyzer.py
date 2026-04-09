import unittest
import math

from app.core.squat_analyzer import analyze_squat_frames
from app.models.pose import Landmark


def _landmark(index: int, x: float, y: float, visibility: float = 0.99) -> Landmark:
    return Landmark(index=index, x=x, y=y, z=0.0, visibility=visibility)


def _frame(knee_angle: float, torso_tilt: float = 5.0) -> list[Landmark]:
    landmarks = [_landmark(index, 0.5, 0.5, visibility=0.1) for index in range(33)]
    knee_center = (0.5, 0.68)
    ankle_center = (0.5, 0.88)

    knee_rad = math.radians(180.0 - knee_angle)
    hip_dx = -0.2 * math.sin(knee_rad)
    hip_dy = -0.2 * math.cos(knee_rad)
    hip_center = (knee_center[0] + hip_dx, knee_center[1] + hip_dy)

    torso_rad = math.radians(torso_tilt)
    shoulder_dx = 0.16 * math.sin(torso_rad)
    shoulder_dy = -0.16 * math.cos(torso_rad)
    shoulder_center = (hip_center[0] + shoulder_dx, hip_center[1] + shoulder_dy)

    coordinates = {
        11: (shoulder_center[0] - 0.03, shoulder_center[1]),
        12: (shoulder_center[0] + 0.03, shoulder_center[1]),
        23: (hip_center[0] - 0.03, hip_center[1]),
        24: (hip_center[0] + 0.03, hip_center[1]),
        25: (knee_center[0] - 0.03, knee_center[1]),
        26: (knee_center[0] + 0.03, knee_center[1]),
        27: (ankle_center[0] - 0.03, ankle_center[1]),
        28: (ankle_center[0] + 0.03, ankle_center[1]),
    }

    for index, (x, y) in coordinates.items():
        landmarks[index] = _landmark(index, x, y)

    return landmarks


class SquatAnalyzerTests(unittest.TestCase):
    def test_analyze_squat_frames_counts_rep(self) -> None:
        frames = [
            _frame(175),
            _frame(160),
            _frame(120),
            _frame(90),
            _frame(85),
            _frame(90),
            _frame(120),
            _frame(160),
            _frame(175),
        ]

        per_frame, summary = analyze_squat_frames(frames)

        self.assertGreaterEqual(summary.reps_detected, 1)
        self.assertGreater(summary.quality_score, 0)
        self.assertIsNotNone(per_frame[-1])

    def test_analyze_squat_frames_flags_low_visibility(self) -> None:
        frame = _frame(175)
        for landmark in frame:
            landmark.visibility = 0.2

        _, summary = analyze_squat_frames([frame, frame, frame])

        self.assertEqual(summary.reps_detected, 0)
        self.assertTrue(
            any(
                "frame" in message.lower() or "lighting" in message.lower()
                for message in summary.feedback
            )
        )


if __name__ == "__main__":
    unittest.main()
