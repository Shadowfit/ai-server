"""스쿼트 실시간/영상 분석. 반복 판정은 squat_counter 한 곳에서 수행한다."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace

from app.core.angle_calculator import extract_angles
from app.core.dtw_calculator import compute_sync_rate
from app.core.squat_counter import DEPTH_MODE_THRESHOLDS, SquatCounter, SquatThresholds
from app.models.pose import Landmark
from app.models.video import SquatAnalysisResult, SquatFrameMetrics
from app.utils.constants import LANDMARK, SYNC_THRESHOLDS

_LOW_CUT_RATIO = 40 / 70


def _mean_landmark(landmarks_by_index: dict[int, Landmark], left_name: str, right_name: str):
    left, right = (landmarks_by_index[LANDMARK[name]] for name in (left_name, right_name))
    return ((left.x + right.x) / 2, (left.y + right.y) / 2)


def _torso_tilt_degrees(landmarks_by_index: dict[int, Landmark]) -> float:
    shoulder_x, shoulder_y = _mean_landmark(
        landmarks_by_index, "LEFT_SHOULDER", "RIGHT_SHOULDER"
    )
    hip_x, hip_y = _mean_landmark(landmarks_by_index, "LEFT_HIP", "RIGHT_HIP")
    return abs(math.degrees(math.atan2(shoulder_x - hip_x, hip_y - shoulder_y + 1e-8)))


def analyze_squat_frames(
    landmark_frames: list[list[Landmark] | None], *,
    bottom_threshold: float = 100.0,
    standing_threshold: float = 160.0,
    fps: float = 3.0,
    timestamps_sec: list[float] | None = None,
    image_aspect_ratio: float = 1.0,
) -> tuple[list[SquatFrameMetrics | None], SquatAnalysisResult]:
    """영상에서도 실시간과 같은 단계 기계를 사용한다.

    fps를 알면 전달해야 한다. 횟수 하한은 초 단위다.
    """
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("fps must be positive")
    if timestamps_sec is not None and len(timestamps_sec) != len(landmark_frames):
        raise ValueError("timestamps must match frames")
    thresholds = replace(SquatThresholds(), bottom_knee=bottom_threshold,
                         standing_knee=standing_threshold)
    counter = SquatCounter(thresholds)
    result_frames: list[SquatFrameMetrics | None] = []
    valid_frames = 0
    deepest = 180.0
    tilt_samples = []
    for i, landmarks in enumerate(landmark_frames):
        when = timestamps_sec[i] if timestamps_sec is not None else i / fps
        obs, _ = counter.update(landmarks, when, image_aspect_ratio)
        if obs is None:
            result_frames.append(None)
            continue
        valid_frames += 1
        deepest = min(deepest, obs.knee_angle)
        tilt_samples.append(obs.torso_tilt)
        result_frames.append(SquatFrameMetrics(
            knee_angle=round(obs.knee_angle, 2), hip_angle=round(obs.hip_angle, 2),
            torso_tilt=round(obs.torso_tilt, 2), hip_height=round(obs.hip_height, 4),
            phase=counter.phase, cycle_stage=counter.stage, rep_count=counter.rep_count,
        ))
    ratio = round(valid_frames / len(landmark_frames), 2) if landmark_frames else 0.
    feedback = []
    if valid_frames == 0:
        feedback.append("관절이 보이는 프레임을 확보하지 못했습니다.")
    elif counter.rep_count == 0:
        feedback.append("서기, 굽히기, 다시 서기로 이어지는 완전한 스쿼트를 감지하지 못했습니다.")
    if valid_frames and ratio < 0.7:
        feedback.append("전신이 보이도록 촬영 위치를 조정해주세요.")
    return result_frames, SquatAnalysisResult(
        reps_detected=counter.rep_count, current_phase=counter.phase,
        deepest_knee_angle=round(deepest, 2) if valid_frames else 0.,
        mean_torso_tilt=round(sum(tilt_samples) / len(tilt_samples), 2) if tilt_samples else 0.,
        quality_score=None, feedback=feedback, valid_frame_ratio=ratio,
    )


@dataclass
class StreamingRepEvent:
    rep_number: int
    sync_rate: float
    feedback_message: str


class StreamingSquatAnalyzer:
    """세션별 SquatCounter를 이용해 한 프레임씩 집계한다.

    기준영상과의 DTW 유사도는 기존 호환 지표이며, 논문의 임상 자세 점수가 아니다.

    🔴 이 분석기 자체는 app/core/analyzer_registry.py가 모듈 로드 시점에 딱 하나 만들어
    모든 세션이 공유하는 stateless 싱글턴이다. 그래서 `self.thresholds`를 세션마다 바꿀 수
    없다 — 대신 스쿼트 깊이 모드(FULL/HALF, 2026-09-21)는 세션별 상태인 `state.depth_mode`에
    저장되고, SquatCounter를 만드는 이 시점에만 그 값을 읽어 임계값을 고른다.
    """

    def __init__(self, exercise_type: str = "squat",
                 thresholds: SquatThresholds | None = None) -> None:
        self.exercise_type = exercise_type
        self.thresholds = thresholds or SquatThresholds()

    def process_frame(self, state, landmarks: list[Landmark], *,
                      timestamp_sec: float | None = None,
                      image_aspect_ratio: float = 1.0
                      ) -> tuple[list[float] | None, float | None, StreamingRepEvent | None]:
        now = time.monotonic() if timestamp_sec is None else timestamp_sec
        if state.squat_counter is None:
            # 세션의 depth_mode로 임계값을 고른다. 인식 못 하는 값(또는 이 필드가 없는 옛
            # 상태 객체)은 이 분석기 자신의 기본값(self.thresholds, 곧 FULL)으로 폴백한다.
            thresholds = DEPTH_MODE_THRESHOLDS.get(
                getattr(state, "depth_mode", "FULL"), self.thresholds
            )
            state.squat_counter = SquatCounter(thresholds, rep_count=state.rep_count)
        counter = state.squat_counter
        before = counter.stage
        obs, completed = counter.update(landmarks, now, image_aspect_ratio)
        state.rep_state = counter.stage
        state.frame_index += 1
        if obs is None:
            state.current_rep_frames.clear()
            return None, None, None
        if before != "waiting_for_standing" and counter.stage == "waiting_for_standing":
            state.current_rep_frames.clear()
        if (before == "ready" and counter.stage == "descending"
                or before == "descending" and counter.stage == "ready" and not completed):
            state.current_rep_frames.clear()
        angles = extract_angles(landmarks, self.exercise_type)
        knee = round(obs.knee_angle, 2)
        state.previous_smoothed_knee = knee
        if not completed:
            return angles, knee, None
        state.rep_count = counter.rep_count
        return angles, knee, self._summarize_rep(state, angles)

    def _summarize_rep(self, state, terminal_angles: list[float]) -> StreamingRepEvent:
        # 최종 선 프레임은 HTTP 경로에서 process_frame 반환 후 버퍼에 들어간다.
        user_angles = [f.angles for f in state.current_rep_frames] + [terminal_angles]
        if state.reference_angles and user_angles:
            try:
                score = compute_sync_rate(state.reference_angles, user_angles)
            except (ValueError, IndexError, TypeError):
                score = 0.
        else:
            score = 0.
        threshold = SYNC_THRESHOLDS.get(state.persona, SYNC_THRESHOLDS["BEGINNER"])
        if score >= threshold:
            message = "자세 양호"
        elif score >= threshold * _LOW_CUT_RATIO:
            message = "자세 보정 필요"
        else:
            message = "즉시 자세 수정 필요"
        return StreamingRepEvent(state.rep_count, score, message)
