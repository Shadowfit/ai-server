"""Repetition boundaries that cannot be inferred from the final count alone."""

from app.core.squat_analyzer import analyze_squat_frames
from app.core.squat_counter import SquatCounter, observe_squat
from app.core.reference_builder import _segment_reps
from app.models.pose import Landmark
from app.models.video import FrameResult
from tests.test_squat_analyzer import _frame


def _run(sequence, fps=3):
    counter = SquatCounter()
    for index, landmarks in enumerate(sequence):
        counter.update(landmarks, index / fps)
    return counter


def _hide_side(frame, side):
    for index in ((11, 23, 25, 27) if side == "left" else (12, 24, 26, 28)):
        frame[index].visibility = 0.1
    return frame


def _shift_side(frame, side, x):
    for index in ((11, 23, 25, 27) if side == "left" else (12, 24, 26, 28)):
        original = frame[index]
        frame[index] = Landmark(index=index, x=original.x + x,
                                y=original.y, z=original.z,
                                visibility=original.visibility)
    return frame


def test_entering_video_at_bottom_does_not_create_a_rep():
    counter = _run([_frame(85)] * 5 + [_frame(175)] * 4)
    assert counter.rep_count == 0
    assert counter.stage == "ready"


def test_partial_depth_does_not_create_a_rep():
    counter = SquatCounter()
    events = []
    for index, landmarks in enumerate([_frame(175)] * 3 + [_frame(120)] * 4 + [_frame(175)] * 4):
        counter.update(landmarks, index / 3)
        if counter.coaching_event:
            events.append(counter.coaching_event)
    assert counter.rep_count == 0
    assert events == ["이번 동작에서 충분히 내려간 장면을 확인하지 못했어요."]


def test_squat_depth_feedback_is_emitted_once_per_completed_rep():
    counter = SquatCounter()
    events = []
    for index, landmarks in enumerate([_frame(175)] * 3 + [_frame(85)] * 4 + [_frame(175)] * 5):
        counter.update(landmarks, index / 3)
        if counter.coaching_event:
            events.append(counter.coaching_event)
    assert counter.rep_count == 1
    assert events == ["스쿼트 깊이를 확인하고 1회 기록했어요."]


def test_lost_tracking_aborts_only_the_inflight_rep():
    rep = [_frame(175)] * 3 + [_frame(85)] * 4 + [_frame(175)] * 4
    counter = _run(rep + [_frame(85)] * 3 + [None] + [_frame(175)] * 4)
    assert counter.rep_count == 1


def test_one_frame_ankle_glitch_does_not_erase_squat_cycle():
    moved = _frame(85)
    for ankle in (27, 28):
        original = moved[ankle]
        moved[ankle] = Landmark(index=ankle, x=original.x + 0.3,
                                y=original.y, z=original.z, visibility=original.visibility)
    counter = _run([_frame(175)] * 3 + [_frame(85)] * 3 + [moved] + [_frame(175)] * 4)
    assert counter.rep_count == 1


def test_foot_repositioning_before_descent_updates_stance_anchor():
    sequence = ([_frame(175)] * 3 +
                [_shift_side(_frame(175), "right", 0.3) for _ in range(3)] +
                [_shift_side(_frame(85), "right", 0.3) for _ in range(4)] +
                [_shift_side(_frame(175), "right", 0.3) for _ in range(4)])
    assert _run(sequence).rep_count == 1


def test_persistent_foot_step_aborts_squat_cycle():
    moved = _frame(85)
    for ankle in (27, 28):
        original = moved[ankle]
        moved[ankle] = Landmark(index=ankle, x=original.x + 0.3,
                                y=original.y, z=original.z, visibility=original.visibility)
    counter = _run([_frame(175)] * 3 + [_frame(85)] * 3 + [moved] * 4 + [_frame(175)] * 4)
    assert counter.rep_count == 0


def test_one_visible_leg_can_complete_a_rep():
    sequence = ([_hide_side(_frame(175), "right") for _ in range(3)] +
                [_hide_side(_frame(85), "right") for _ in range(4)] +
                [_hide_side(_frame(175), "right") for _ in range(4)])
    counter = _run(sequence)
    assert counter.rep_count == 1
    assert observe_squat(sequence[0], 1.0, 0.55).visible_sides == (True, False)


def test_switching_the_only_visible_leg_aborts_inflight_rep():
    sequence = ([_hide_side(_frame(175), "right") for _ in range(3)] +
                [_hide_side(_frame(85), "right") for _ in range(3)] +
                [_hide_side(_frame(85), "left") for _ in range(3)] +
                [_hide_side(_frame(175), "left") for _ in range(4)])
    assert _run(sequence).rep_count == 0


def test_visible_leg_persistent_step_still_aborts_squat_cycle():
    moved = _hide_side(_frame(85), "right")
    original = moved[27]
    moved[27] = Landmark(index=27, x=original.x + 0.3,
                         y=original.y, z=original.z, visibility=original.visibility)
    sequence = ([_hide_side(_frame(175), "right") for _ in range(3)] +
                [_hide_side(_frame(85), "right") for _ in range(3)] +
                [moved] * 4 +
                [_hide_side(_frame(175), "right") for _ in range(4)])
    assert _run(sequence).rep_count == 0


def test_both_leg_chains_occluded_still_abort_inflight_rep():
    hidden = _frame(85)
    hidden[25].visibility = hidden[26].visibility = 0.1
    counter = _run([_frame(175)] * 3 + [_frame(85)] * 3 +
                   [hidden] + [_frame(175)] * 4)
    assert counter.rep_count == 0


def test_reference_segments_follow_completed_repetitions():
    sequence = [_frame(175)] * 3 + [_frame(85)] * 4 + [_frame(175)] * 4
    metrics, summary = analyze_squat_frames(sequence, fps=3)
    frames = [FrameResult(frame_index=i, timestamp=i / 3, landmarks=landmarks,
                          angles=[float(i)], squat_metrics=metrics[i])
              for i, landmarks in enumerate(sequence)]
    segments = _segment_reps(frames)
    assert summary.reps_detected == 1
    assert len(segments) == 1
    assert segments[0].rep_index == 1
    # At fps=3 the sample gap (1/3 s) already exceeds standing_confirm_sec (0.2s),
    # so the very first standing sample after the bottom (frame 7) is itself enough
    # dwell evidence and completes the rep -- a second standing sample is no longer
    # required. See squat-repetition-counting.md's time-based-sampling note.
    assert segments[0].end_frame_index == 7


# ── 스쿼트 깊이 모드 (FULL/HALF, 2026-09-21) ────────────────────────────────
# HALF_SQUAT_THRESHOLDS의 bottom_hip/min_hip_drop은 실촬영 검증 전이라(VALIDATE 주석
# 참고) 여기서는 "FSM이 두 모드를 다르게 취급하는가"만 고정한다 — 정확한 깊이 판정
# 기준 자체의 검증은 docs/verification/의 하프 스쿼트 실촬영 리포트가 담당한다.

from app.core.squat_counter import DEPTH_MODE_THRESHOLDS, HALF_SQUAT_THRESHOLDS  # noqa: E402


def test_half_squat_thresholds_are_internally_consistent():
    # __post_init__이 통과한다는 것 자체가 검증 — 생성이 실패하면 여기서 예외가 난다.
    assert HALF_SQUAT_THRESHOLDS.bottom_knee + HALF_SQUAT_THRESHOLDS.bottom_knee_margin == 140.0
    assert DEPTH_MODE_THRESHOLDS["FULL"].bottom_knee == 100.0
    assert DEPTH_MODE_THRESHOLDS["HALF"] is HALF_SQUAT_THRESHOLDS


def test_half_depth_completes_under_half_thresholds_but_not_full_thresholds():
    # 무릎 130도까지만 내려가는 시퀀스 — 하프 밴드(115~140) 안, 풀 스쿼트 기준(<=115)엔 못 미침.
    sequence = [_frame(175)] * 3 + [_frame(130)] * 4 + [_frame(175)] * 4

    half_counter = SquatCounter(HALF_SQUAT_THRESHOLDS)
    for index, landmarks in enumerate(sequence):
        half_counter.update(landmarks, index / 3)
    assert half_counter.rep_count == 1
    assert half_counter.stage == "ready"

    full_counter = SquatCounter(DEPTH_MODE_THRESHOLDS["FULL"])
    for index, landmarks in enumerate(sequence):
        full_counter.update(landmarks, index / 3)
    assert full_counter.rep_count == 0
    # 바닥 조건(무릎<=115)을 한 번도 못 만족해 "bottom" 단계에 들어가지 못한다 —
    # test_partial_depth_does_not_create_a_rep과 같은 경로(얕음 피드백)를 탄다.
    assert full_counter.stage == "ready"
