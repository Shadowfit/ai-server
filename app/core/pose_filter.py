"""Post-processing filters for MediaPipe pose landmarks."""

from __future__ import annotations

from dataclasses import dataclass

from app.models.pose import Landmark
from app.utils.constants import LANDMARK


SQUAT_TRACKED_POINTS = (
    "LEFT_SHOULDER",
    "RIGHT_SHOULDER",
    "LEFT_HIP",
    "RIGHT_HIP",
    "LEFT_KNEE",
    "RIGHT_KNEE",
    "LEFT_ANKLE",
    "RIGHT_ANKLE",
)


@dataclass(frozen=True)
class NormalizedRoi:
    """Normalized region of interest used to keep the tracked person centered."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    def contains(self, x: float, y: float) -> bool:
        return self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y


DEFAULT_SQUAT_ROI = NormalizedRoi(min_x=0.38, min_y=0.4, max_x=0.78, max_y=0.76)


@dataclass(frozen=True)
class PoseBounds:
    """Normalized body bounds for the landmarks used by the squat analyzer."""

    center_x: float
    center_y: float
    width: float
    height: float
    average_visibility: float

    @property
    def aspect_ratio(self) -> float:
        return self.width / (self.height + 1e-8)


def get_squat_pose_bounds(landmarks: list[Landmark]) -> PoseBounds:
    """Return bounds for core lower-body squat landmarks."""
    tracked = [landmarks[LANDMARK[name]] for name in SQUAT_TRACKED_POINTS]
    xs = [landmark.x for landmark in tracked]
    ys = [landmark.y for landmark in tracked]
    visibility = [landmark.visibility for landmark in tracked]
    return PoseBounds(
        center_x=sum(xs) / len(xs),
        center_y=sum(ys) / len(ys),
        width=max(xs) - min(xs),
        height=max(ys) - min(ys),
        average_visibility=sum(visibility) / len(visibility),
    )


def is_plausible_squat_pose(
    landmarks: list[Landmark],
    *,
    min_visibility: float = 0.65,
    min_height: float = 0.2,
    min_width: float = 0.1,
    max_width: float = 0.34,
    max_aspect_ratio: float = 0.9,
    roi: NormalizedRoi | None = DEFAULT_SQUAT_ROI,
) -> bool:
    """Reject common rack/equipment false positives before squat analysis."""
    bounds = get_squat_pose_bounds(landmarks)
    if bounds.average_visibility < min_visibility:
        return False
    if bounds.height < min_height or bounds.width < min_width:
        return False
    if bounds.width > max_width:
        return False
    if bounds.aspect_ratio > max_aspect_ratio:
        return False
    if roi and not roi.contains(bounds.center_x, bounds.center_y):
        return False
    return True


class PoseTargetTracker:
    """Keep pose analysis locked on one plausible person across video frames."""

    def __init__(
        self,
        max_center_jump: float = 0.28,
        roi: NormalizedRoi | None = DEFAULT_SQUAT_ROI,
    ) -> None:
        self.max_center_jump = max_center_jump
        self.roi = roi
        self._last_center: tuple[float, float] | None = None

    def filter(self, landmarks: list[Landmark] | None) -> list[Landmark] | None:
        """Return landmarks only when they look like the tracked squat subject."""
        if landmarks is None or not is_plausible_squat_pose(landmarks, roi=self.roi):
            return None

        bounds = get_squat_pose_bounds(landmarks)
        center = (bounds.center_x, bounds.center_y)
        if self._last_center is not None:
            dx = center[0] - self._last_center[0]
            dy = center[1] - self._last_center[1]
            distance = (dx * dx + dy * dy) ** 0.5
            if distance > self.max_center_jump:
                return None

        self._last_center = center
        return landmarks
