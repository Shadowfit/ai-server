"""Heuristic squat analysis built on top of pose landmarks."""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.core.angle_calculator import calculate_angle, extract_angles
from app.core.dtw_calculator import compute_sync_rate
from app.models.pose import Landmark
from app.models.video import SquatAnalysisResult, SquatFrameMetrics
from app.utils.constants import LANDMARK, SYNC_THRESHOLDS

# 즉시 수정 필요 컷 / 양호 컷 비율. 기존 고정값(양호>=70, 즉시수정<40)의 비율을 유지한 채
# 페르소나별 "양호" 기준(SYNC_THRESHOLDS)에 비례 스케일한다.
_LOW_CUT_RATIO = 40 / 70


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
    """rep 1회가 완성될 때마다 발행되는 이벤트.

    ⚠️ deepest_knee_angle · mean_torso_tilt 를 제거했다(이슈 #85). 둘 다 배치 경로의
    SquatAnalysisResult 에서 이름째 가져온 필드인데, 배치는 그 값을 HTTP 응답으로 실제로
    돌려주는 반면 스트리밍은 pose.py 가 sync_rate·feedback_message·rep_number 만 읽어
    **한 번도 소비되지 않았다.** 소비처가 없으니 테스트도 붙지 않았고, 그래서 결함 2건이
    그대로 남아 있었다:

    - mean_torso_tilt 는 평균이 아니었다 — 삼항 연산자의 양쪽이 글자까지 같아 어느 쪽으로
      가도 마지막 프레임 한 점이었다. rep 의 마지막 프레임은 정의상 다시 선 자세라
      "그 rep 동안 얼마나 기울었나"에 가장 안 기운 순간으로 답하고 있었다
    - deepest_knee_angle 은 angles[0] 즉 **왼쪽 무릎만** 봤다. rep 경계를 판정하는 값은
      좌우 평균인데(_extract_raw_metrics) 여기만 왼쪽이라, 한쪽으로 기우는 자세에서 둘이 갈렸다

    고치는 대신 지운 이유: 프레임별 깊이 지표(PerRepFrame.smoothed_knee_angle)를 Spring 으로
    직접 보내게 되면서 rep 요약본이 필요 없어졌다. Spring 이 프레임들에서 직접 최소값을 고르므로
    같은 사실이 두 곳에 저장되지 않는다. 배치 경로의 SquatAnalysisResult 는 소비처가 있으므로
    그대로 둔다.
    """

    rep_number: int
    sync_rate: float
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

    # bottom 체류 상한 — 이만큼 넘게 앉아 있었으면 스쿼트가 아니라 "쉬고 있었다"로 본다
    # (이슈 #93). MIN_REP_FRAMES 가 하한이라면 이건 대칭이 되는 상한이다.
    #
    # 3fps 기준 5초. 실제 스쿼트의 바닥 체류는 1초 내외라 5배 여유이고, 홀드 스쿼트 같은
    # 변형까지 감안해도 넉넉하다. 오판 방향도 안전한 쪽이다 — 크게 잡으면 "덜 걸러짐"이지만
    # 작게 잡으면 정상 rep 이 사라진다.
    #
    # 왜 하강 속도가 아니라 체류 시간인가: 의자에 5초에 걸쳐 앉으면 프레임당 ~5.6° 라
    # descending 기준(delta <= -4)을 그냥 통과한다. 임계를 더 빡세게 잡으면 이번엔 천천히
    # 하는 초보자·재활 사용자의 정상 rep 이 걸린다. 두 행동은 속도 축에서 분리되지 않고,
    # 바닥 체류 시간 축에서만 갈린다(스쿼트 ~1초 vs 앉아쉼 30~90초).
    MAX_BOTTOM_FRAMES = 15

    def __init__(self, exercise_type: str = "squat") -> None:
        self.exercise_type = exercise_type

    def process_frame(
        self,
        state,
        landmarks: list[Landmark],
    ) -> tuple[list[float] | None, float | None, StreamingRepEvent | None]:
        """단일 프레임을 처리.

        Returns:
            (angles, smoothed_knee_angle, rep_event) — angles는 visibility 통과 시 계산된
            각도 시퀀스, smoothed_knee_angle은 rep 경계 판정에 쓰는 것과 같은 값(좌우 평균을
            3프레임 평활)으로 프레임별 깊이 지표다, rep_event는 이 프레임으로 rep 1회가
            완성된 경우에만 채워진다.

        깊이 지표를 state가 아니라 반환값으로 내보내는 이유: state.previous_smoothed_knee가
        이미 같은 값을 담지만 그 이름은 "직전 프레임"을 뜻한다. 호출자 입장에서는 방금 처리한
        프레임의 값이라 이름과 어긋나고, 이 프로젝트는 그런 자리에서 결함이 반복해 나왔다
        (#78·#79·#80·#85).
        """
        if not landmarks or _frame_visibility_score(landmarks) < self.VISIBILITY_FLOOR:
            state.frame_index += 1
            return None, None, None

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
                state.bottom_entry_frame_index = state.frame_index
        elif state.rep_state == "ready" and smooth_knee <= self.BOTTOM_THRESHOLD:
            state.rep_state = "bottom"
            state.bottom_entry_frame_index = state.frame_index
        elif state.rep_state == "bottom" and smooth_knee >= self.STANDING_THRESHOLD:
            if state.frame_index - state.bottom_entry_frame_index > self.MAX_BOTTOM_FRAMES:
                # 바닥에 오래 머물렀다 — 운동이 아니라 휴식이었다(이슈 #93).
                # rep 으로 세지 않되 상태는 되돌린다. 안 그러면 bottom 에 갇혀 다음 진짜
                # rep 을 놓친다 — "일어섰다"는 사실 자체는 그대로 반영해야 한다.
                state.rep_state = "ready"
                # 이 구간의 프레임은 운동이 아니므로 다음 rep 에 섞이면 안 된다. 두면
                # _summarize_rep 이 휴식 90프레임까지 넣고 DTW 를 돌려 sync_rate 가
                # 오염된다. (버퍼가 무한정 자라는 문제 자체는 별건 — 이슈 #91)
                state.current_rep_frames.clear()
            elif state.frame_index - state.last_rep_frame_index >= self.MIN_REP_FRAMES:
                state.rep_count += 1
                state.rep_state = "ready"
                state.last_rep_frame_index = state.frame_index
                # 🔀 머지 해소(main ← feat/deepest-frame-resolution): 제어흐름은 main 의
                #    것(#93 휴식 감지)을 그대로 두고, 호출만 1인자로 되돌린다. main 이
                #    넘기던 last_raw 는 _summarize_rep 안에서 deepest_knee_angle ·
                #    mean_torso_tilt 를 만드는 데만 쓰였는데, 이 브랜치가 그 두 필드를
                #    **삭제**했다(이슈 #85 — 소비처가 없어 결함 2건이 잠복해 있었다).
                #    인자를 남기면 정의(1인자)와 어긋나 TypeError 가 난다.
                rep_event = self._summarize_rep(state)
            # else: MIN_REP_FRAMES 미달 — 원래대로 bottom 을 유지한다. 이 하한은 rep 을
            # 거부하는 게 아니라 늦추는 장치라, 서 있는 동안 다음 프레임에서 다시 판정된다.

        state.previous_smoothed_knee = smooth_knee
        state.frame_index += 1
        return angles, smooth_knee, rep_event

    def _summarize_rep(self, state) -> StreamingRepEvent:
        """현재까지 누적된 current_rep_frames로 rep 결과 산출."""
        user_angle_seq = [f.angles for f in state.current_rep_frames]

        if state.reference_angles and user_angle_seq:
            try:
                sync_rate = compute_sync_rate(state.reference_angles, user_angle_seq)
            except Exception:
                sync_rate = 0.0
        else:
            sync_rate = 0.0

        pass_threshold = SYNC_THRESHOLDS.get(state.persona, SYNC_THRESHOLDS["BEGINNER"])
        low_threshold = pass_threshold * _LOW_CUT_RATIO

        if sync_rate >= pass_threshold:
            msg = "자세 양호"
        elif sync_rate >= low_threshold:
            msg = "자세 보정 필요"
        else:
            msg = "즉시 자세 수정 필요"

        return StreamingRepEvent(
            rep_number=state.rep_count,
            sync_rate=sync_rate,
            feedback_message=msg,
        )
