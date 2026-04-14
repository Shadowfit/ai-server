import unittest

from app.core.pose_filter import (
    DEFAULT_SQUAT_ROI,
    NormalizedRoi,
    PoseTargetTracker,
    get_squat_pose_bounds,
    is_plausible_squat_pose,
)
from app.models.pose import Landmark


def _landmark(index: int, x: float, y: float, visibility: float = 0.9) -> Landmark:
    return Landmark(index=index, x=x, y=y, z=0.0, visibility=visibility)


def _frame(
    *,
    center_x: float = 0.55,
    center_y: float = 0.6,
    width: float = 0.18,
    height: float = 0.55,
    visibility: float = 0.9,
) -> list[Landmark]:
    landmarks = [_landmark(index, center_x, center_y, visibility=0.1) for index in range(33)]
    half_width = width / 2.0
    top = center_y - height / 2.0
    bottom = center_y + height / 2.0

    coordinates = {
        11: (center_x - half_width, top),
        12: (center_x + half_width, top),
        23: (center_x - half_width * 0.7, center_y - height * 0.05),
        24: (center_x + half_width * 0.7, center_y - height * 0.05),
        25: (center_x - half_width * 0.6, center_y + height * 0.25),
        26: (center_x + half_width * 0.6, center_y + height * 0.25),
        27: (center_x - half_width * 0.5, bottom),
        28: (center_x + half_width * 0.5, bottom),
    }
    for index, (x, y) in coordinates.items():
        landmarks[index] = _landmark(index, x, y, visibility)
    return landmarks


class PoseFilterTests(unittest.TestCase):
    def test_plausible_squat_pose_accepts_full_body_side_view(self) -> None:
        landmarks = _frame()

        bounds = get_squat_pose_bounds(landmarks)

        self.assertTrue(is_plausible_squat_pose(landmarks))
        self.assertGreater(bounds.height, 0.34)
        self.assertLess(bounds.aspect_ratio, 0.9)

    def test_plausible_squat_pose_rejects_small_equipment_detection(self) -> None:
        landmarks = _frame(center_x=0.82, width=0.21, height=0.21, visibility=0.95)

        self.assertFalse(is_plausible_squat_pose(landmarks))

    def test_plausible_squat_pose_rejects_thin_edge_detection(self) -> None:
        landmarks = _frame(center_x=0.96, width=0.05, height=0.32, visibility=0.86)

        self.assertFalse(is_plausible_squat_pose(landmarks))

    def test_plausible_squat_pose_rejects_rack_mixed_wide_detection(self) -> None:
        landmarks = _frame(center_x=0.48, width=0.36, height=0.52, visibility=0.7)

        self.assertFalse(is_plausible_squat_pose(landmarks))

    def test_plausible_squat_pose_rejects_low_rack_mixed_detection(self) -> None:
        landmarks = _frame(center_x=0.55, center_y=0.8, width=0.18, height=0.36)

        self.assertFalse(is_plausible_squat_pose(landmarks))

    def test_plausible_squat_pose_rejects_pose_outside_default_roi(self) -> None:
        landmarks = _frame(center_x=0.2, center_y=0.62, width=0.18, height=0.55)

        self.assertFalse(is_plausible_squat_pose(landmarks))

    def test_plausible_squat_pose_allows_custom_wide_roi(self) -> None:
        landmarks = _frame(center_x=0.2, center_y=0.62, width=0.18, height=0.55)
        wide_roi = NormalizedRoi(min_x=0.15, min_y=0.4, max_x=0.85, max_y=0.8)

        self.assertFalse(DEFAULT_SQUAT_ROI.contains(0.2, 0.62))
        self.assertTrue(is_plausible_squat_pose(landmarks, roi=wide_roi))

    def test_tracker_rejects_large_center_jump(self) -> None:
        tracker = PoseTargetTracker(max_center_jump=0.2)

        self.assertIsNotNone(tracker.filter(_frame(center_x=0.55)))
        self.assertIsNone(tracker.filter(_frame(center_x=0.2)))


if __name__ == "__main__":
    unittest.main()
