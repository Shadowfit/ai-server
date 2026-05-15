"""Heuristic squat analysis built on top of pose landmarks."""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.core.angle_calculator import calculate_angle, extract_angles
from app.core.dtw_calculator import compute_sync_rate
from app.models.pose import Landmark
from app.models.video import SquatAnalysisResult, SquatFrameMetrics
from app.utils.constants import LANDMARK


@dataclass
class _RawSquatFrame:
    knee_angle: float
    hip_angle: float
    torso_tilt: float
    hip_height: float


def _mean_landmark(
    landmarks_by_index: dict[int, Landmark], left_name: str, right_name: str
) -> tuple[float, float]:
    left = landmarks_by_index[LANDMARK[left_name]]
    right = landmarks_by_index[LANDMARK[right_name]]
    return ((left.x + right.x) / 2.0, (left.y + right.y) / 2.0)


def _torso_tilt_degrees(landmarks_by_index: dict[int, Landmark]) -> float:
    shoulder_x, shoulder_y = _mean_landmark(
        landmarks_by_index, "LEFT_SHOULDER", "RIGHT_SHOULDER"
    )
    hip_x, hip_y = _mean_landmark(landmarks_by_index, "LEFT_HIP", "RIGHT_HIP")
    dx = shoulder_x - hip_x
    dy = hip_y - shoulder_y
    return abs(math.degrees(math.atan2(dx, dy + 1e-8)))


def _frame_visibility_score(landmarks: list[Landmark]) -> float:
    tracked_points = (
        "LEFT_SHOULDER",
        "RIGHT_SHOULDER",
        "LEFT_HIP",
        "RIGHT_HIP",
        "LEFT_KNEE",
        "RIGHT_KNEE",
        "LEFT_ANKLE",
        "RIGHT_ANKLE",
    )
    scores = [landmarks[LANDMARK[name]].visibility for name in tracked_points]
    return sum(scores) / len(scores)


def _extract_raw_metrics(landmarks: list[Landmark]) -> _RawSquatFrame:
    lm_map = {lm.index: lm for lm in landmarks}

    left_knee_angle = calculate_angle(
        lm_map[LANDMARK["LEFT_HIP"]],
        lm_map[LANDMARK["LEFT_KNEE"]],
        lm_map[LANDMARK["LEFT_ANKLE"]],
    )
    right_knee_angle = calculate_angle(
        lm_map[LANDMARK["RIGHT_HIP"]],
        lm_map[LANDMARK["RIGHT_KNEE"]],
        lm_map[LANDMARK["RIGHT_ANKLE"]],
    )
    left_hip_angle = calculate_angle(
        lm_map[LANDMARK["LEFT_SHOULDER"]],
        lm_map[LANDMARK["LEFT_HIP"]],
        lm_map[LANDMARK["LEFT_KNEE"]],
    )
    right_hip_angle = calculate_angle(
        lm_map[LANDMARK["RIGHT_SHOULDER"]],
        lm_map[LANDMARK["RIGHT_HIP"]],
        lm_map[LANDMARK["RIGHT_KNEE"]],
    )

    left_hip = lm_map[LANDMARK["LEFT_HIP"]]
    right_hip = lm_map[LANDMARK["RIGHT_HIP"]]

    return _RawSquatFrame(
        knee_angle=round((left_knee_angle + right_knee_angle) / 2.0, 2),
        hip_angle=round((left_hip_angle + right_hip_angle) / 2.0, 2),
        torso_tilt=round(_torso_tilt_degrees(lm_map), 2),
        hip_height=round((left_hip.y + right_hip.y) / 2.0, 4),
    )


def _phase_from_angles(current_angle: float, delta: float) -> str:
    if current_angle <= 95:
        return "bottom"
    if current_angle >= 155:
        return "standing"
    if delta <= -4:
        return "descending"
    if delta >= 4:
        return "ascending"
    return "transition"


def analyze_squat_frames(
    landmark_frames: list[list[Landmark] | None],
    *,
    bottom_threshold: float = 100.0,
    standing_threshold: float = 150.0,
    min_rep_frames: int = 4,
) -> tuple[list[SquatFrameMetrics | None], SquatAnalysisResult]:
    """Analyze a sequence of landmark frames and infer squat reps and feedback."""
    raw_metrics: list[_RawSquatFrame | None] = []
    valid_frames = 0

    for landmarks in landmark_frames:
        if not landmarks or _frame_visibility_score(landmarks) < 0.55:
            raw_metrics.append(None)
            continue
        raw_metrics.append(_extract_raw_metrics(landmarks))
        valid_frames += 1

    smoothed_knees: list[float | None] = []
    for index, metric in enumerate(raw_metrics):
        if metric is None:
            smoothed_knees.append(None)
            continue
        window = [
            candidate.knee_angle
            for candidate in raw_metrics[max(0, index - 1) : index + 2]
            if candidate is not None
        ]
        smoothed_knees.append(round(sum(window) / len(window), 2))

    frame_metrics: list[SquatFrameMetrics | None] = []
    previous_angle: float | None = None
    rep_count = 0
    rep_state = "waiting_for_standing"
    last_rep_frame_index = -10_000
    deepest_knee = 180.0
    torso_samples: list[float] = []
    current_phase = "unknown"

    for frame_index, (metric, smooth_knee) in enumerate(
        zip(raw_metrics, smoothed_knees, strict=False)
    ):
        if metric is None or smooth_knee is None:
            frame_metrics.append(None)
            continue

        delta = 0.0 if previous_angle is None else smooth_knee - previous_angle
        phase = _phase_from_angles(smooth_knee, delta)

        if rep_state == "waiting_for_standing":
            if smooth_knee >= standing_threshold:
                rep_state = "ready"
            elif smooth_knee <= bottom_threshold:
                rep_state = "bottom"
        elif rep_state == "ready" and smooth_knee <= bottom_threshold:
            rep_state = "bottom"
        elif (
            rep_state == "bottom"
            and smooth_knee >= standing_threshold
            and frame_index - last_rep_frame_index >= min_rep_frames
        ):
            rep_count += 1
            rep_state = "ready"
            last_rep_frame_index = frame_index

        previous_angle = smooth_knee
        deepest_knee = min(deepest_knee, smooth_knee)
        torso_samples.append(metric.torso_tilt)
        current_phase = phase

        frame_metrics.append(
            SquatFrameMetrics(
                knee_angle=smooth_knee,
                hip_angle=metric.hip_angle,
                torso_tilt=metric.torso_tilt,
                hip_height=metric.hip_height,
                phase=phase,
                rep_count=rep_count,
            )
        )

    valid_ratio = round(valid_frames / len(landmark_frames), 2) if landmark_frames else 0.0
    mean_torso = round(sum(torso_samples) / len(torso_samples), 2) if torso_samples else 0.0

    feedback: list[str] = []
    quality_score = 100

    if valid_ratio < 0.7:
        feedback.append("Keep the full body in frame and improve lighting for steadier tracking.")
        quality_score -= 20
    if deepest_knee > 115:
        feedback.append("Go slightly deeper so the hip drops closer to knee level.")
        quality_score -= 20
    if mean_torso > 35:
        feedback.append("Lift the chest more to reduce excessive forward lean.")
        quality_score -= 15
    if rep_count == 0 and valid_frames > 0:
        feedback.append(
            "No full squat rep was detected. Start from standing, go below parallel, and stand tall again."
        )
        quality_score -= 25

    if not feedback and valid_frames > 0:
        feedback.append("Stable squat pattern detected. This video is good for a live demo.")

    summary = SquatAnalysisResult(
        reps_detected=rep_count,
        current_phase=current_phase,
        deepest_knee_angle=round(deepest_knee if deepest_knee < 180 else 0.0, 2),
        mean_torso_tilt=mean_torso,
        quality_score=max(0, quality_score),
        feedback=feedback,
        valid_frame_ratio=valid_ratio,
    )
    return frame_metrics, summary


# ---------------------------------------------------------------------------
# Streaming(실시간) 분석기
# ---------------------------------------------------------------------------


@dataclass
class StreamingRepEvent:
    """rep 1회가 완성될 때마다 발행되는 이벤트."""

    rep_number: int
    sync_rate: float
    deepest_knee_angle: float
    mean_torso_tilt: float
    feedback_message: str


class StreamingSquatAnalyzer:
    """프레임 단위로 호출되는 stateful squat 분석기.

    각 호출에서 (rep_state, smoothing window)를 SessionState 안에 보존하고,
    rep 1회 완성 시 그 rep 구간의 user 각도들과 reference angle sequence를
    DTW로 비교해 sync_rate를 산출한다.
    """

    BOTTOM_THRESHOLD = 100.0
    STANDING_THRESHOLD = 150.0
    MIN_REP_FRAMES = 4
    VISIBILITY_FLOOR = 0.55

    def __init__(self, exercise_type: str = "squat") -> None:
        self.exercise_type = exercise_type

    def process_frame(
        self,
        state,
        landmarks: list[Landmark],
    ) -> tuple[list[float] | None, StreamingRepEvent | None]:
        """단일 프레임을 처리.

        Returns:
            (angles, rep_event) — angles는 visibility 통과 시 계산된 각도 시퀀스,
            rep_event는 이 프레임으로 rep 1회가 완성된 경우에만 채워진다.
        """
        if not landmarks or _frame_visibility_score(landmarks) < self.VISIBILITY_FLOOR:
            state.frame_index += 1
            return None, None

        raw = _extract_raw_metrics(landmarks)
        angles = extract_angles(landmarks, self.exercise_type)

        # 최근 3개 raw knee로 smoothing
        state.recent_raw_knees.append(raw.knee_angle)
        if len(state.recent_raw_knees) > 3:
            state.recent_raw_knees.pop(0)
        smooth_knee = round(sum(state.recent_raw_knees) / len(state.recent_raw_knees), 2)

        rep_event: StreamingRepEvent | None = None

        if state.rep_state == "waiting_for_standing":
            if smooth_knee >= self.STANDING_THRESHOLD:
                state.rep_state = "ready"
            elif smooth_knee <= self.BOTTOM_THRESHOLD:
                state.rep_state = "bottom"
        elif state.rep_state == "ready" and smooth_knee <= self.BOTTOM_THRESHOLD:
            state.rep_state = "bottom"
        elif (
            state.rep_state == "bottom"
            and smooth_knee >= self.STANDING_THRESHOLD
            and state.frame_index - state.last_rep_frame_index >= self.MIN_REP_FRAMES
        ):
            state.rep_count += 1
            state.rep_state = "ready"
            state.last_rep_frame_index = state.frame_index
            rep_event = self._summarize_rep(state, raw)

        state.previous_smoothed_knee = smooth_knee
        state.frame_index += 1
        return angles, rep_event

    def _summarize_rep(self, state, last_raw) -> StreamingRepEvent:
        """현재까지 누적된 current_rep_frames로 rep 결과 산출."""
        user_angle_seq = [f.angles for f in state.current_rep_frames]

        if state.reference_angles and user_angle_seq:
            try:
                sync_rate = compute_sync_rate(state.reference_angles, user_angle_seq)
            except Exception:
                sync_rate = 0.0
        else:
            sync_rate = 0.0

        knees = [a[0] for a in user_angle_seq if a] or [last_raw.knee_angle]
        deepest = round(min(knees), 2)
        torsos = [last_raw.torso_tilt] if not state.current_rep_frames else [last_raw.torso_tilt]
        mean_torso = round(sum(torsos) / len(torsos), 2)

        if sync_rate >= 70:
            msg = "자세 양호"
        elif sync_rate >= 40:
            msg = "자세 보정 필요"
        else:
            msg = "즉시 자세 수정 필요"

        return StreamingRepEvent(
            rep_number=state.rep_count,
            sync_rate=sync_rate,
            deepest_knee_angle=deepest,
            mean_torso_tilt=mean_torso,
            feedback_message=msg,
        )
