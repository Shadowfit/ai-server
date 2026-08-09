"""bottom 체류 상한이 «체류» 를 재는가, 아니면 «밴드 통과 + 체류» 를 재는가 (이슈 #159).

결함: 진입 임계는 100° 인데 이탈 임계는 150° 다. 예전에는 그 둘 사이의 **프레임 인덱스 차이**를
`MAX_BOTTOM_FRAMES` 와 비교했으므로, 상승 중 100~150° 를 지나는 시간이 통째로 「바닥 체류」에
들어갔다. 밴드 통과 시간은 하강·상승 속도에 정비례하므로 **속도를 안 보려고 만든 상수가 속도에
의존**하게 된다 — 그리고 주석([`squat_analyzer.py`] MAX_BOTTOM_FRAMES)이 이 상수를 넣은 이유로
든 것이 정확히 그 사용자(천천히 하는 초보자·재활)다.

측정(#159): 3fps 고정 · 체류 0.5초 고정에서 **하강·상승 5.1초부터 정상 rep 이 사라졌다.**

수정: 프레임 인덱스 차이 대신 `state.bottom_frame_count` — 실제로 100° 아래에 있던 프레임 수 —
를 센다. 새 임계를 만들지 않으려고 기존 `BOTTOM_THRESHOLD` 를 재사용했고 값 15 도 그대로다.

⚠️ 이 결함은 **#143 과 독립**이다. 아래 케이스는 전부 3fps 이고, 3fps 는 유입 상한(3.33fps)을
   아예 타지 않는다. fps 를 고정해도 남는다는 것이 이 이슈의 핵심이다.

⚠️ 궤적은 **합성**이다(하강·상승 선형 보간). #143·#159 의 원 측정과 같은 한계를 물려받는다 —
   실제 스쿼트 각도 로그로 재확인한 것이 아니다.
"""
import unittest

from app.core.squat_analyzer import StreamingSquatAnalyzer
from app.grpc.session_state import PerRepFrame, SessionState

from tests.test_squat_analyzer import _frame

_STANDING_ANGLE = 170.0
_BOTTOM_ANGLE = 85.0
_LEAD_SEC = 1.0
_TAIL_SEC = 1.5

# 현재 클라의 전송 간격 (frontend exercise.tsx intervalMs=330). 이 이슈의 요점이 «fps 를
# 고정해도 남는다» 이므로 fps 는 상수로 못박는다.
_FPS = 3.0


def _knee_angle_at(t: float, descent: float, hold: float, ascent: float) -> float:
    descent_start = _LEAD_SEC
    hold_start = descent_start + descent
    ascent_start = hold_start + hold
    ascent_end = ascent_start + ascent

    if t < descent_start:
        return _STANDING_ANGLE
    if t < hold_start:
        ratio = (t - descent_start) / descent
        return _STANDING_ANGLE + (_BOTTOM_ANGLE - _STANDING_ANGLE) * ratio
    if t < ascent_start:
        return _BOTTOM_ANGLE
    if t < ascent_end:
        ratio = (t - ascent_start) / ascent
        return _BOTTOM_ANGLE + (_STANDING_ANGLE - _BOTTOM_ANGLE) * ratio
    return _STANDING_ANGLE


def _count_reps(descent: float, hold: float, ascent: float | None = None) -> int:
    """하강/체류/상승을 초 단위로 준 궤적을 3fps 로 샘플링해 rep 수를 돌려준다."""
    if ascent is None:
        ascent = descent

    state = SessionState(session_id=1, exercise_id=1)
    analyzer = StreamingSquatAnalyzer("squat")
    reps = 0

    total = _LEAD_SEC + descent + hold + ascent + _TAIL_SEC
    for k in range(int(total * _FPS) + 1):
        now = k / _FPS
        angles, _, rep_event = analyzer.process_frame(
            state, _frame(_knee_angle_at(now, descent, hold, ascent))
        )
        if angles is not None:
            state.current_rep_frames.append(
                PerRepFrame(timestamp_sec=now, joint_coordinates="{}", angles=angles)
            )
        if rep_event is not None:
            reps += 1
            state.current_rep_frames.clear()

    return reps


class SlowUserTests(unittest.TestCase):
    """하강이 느린 사용자의 정상 rep 이 살아남는가 — #159 가 신고한 실패."""

    # 체류 0.5초는 «짧다». 이 케이스들은 전부 휴식이 아니라 정상 스쿼트다.
    SHORT_HOLD = 0.5

    def test_slow_descent_still_counts(self) -> None:
        """하강·상승 9초짜리 재활 속도 스쿼트도 집계된다 (변경 전 5.1초부터 사라졌다)."""
        for pace in (1.2, 3.0, 5.0, 7.0, 9.0):
            with self.subTest(descent_sec=pace):
                self.assertEqual(
                    _count_reps(pace, self.SHORT_HOLD),
                    1,
                    f"하강·상승 {pace}s · 체류 {self.SHORT_HOLD}s 는 정상 rep 인데 사라졌다",
                )

    def test_asymmetric_pace_counts(self) -> None:
        """천천히 내려가 빠르게 일어서는 흔한 패턴도 집계된다."""
        self.assertEqual(_count_reps(6.0, self.SHORT_HOLD, ascent=1.2), 1)
        self.assertEqual(_count_reps(1.2, self.SHORT_HOLD, ascent=6.0), 1)

    def test_hold_squat_counts(self) -> None:
        """5초 홀드 스쿼트도 rep 이다.

        주석은 «홀드 스쿼트 같은 변형까지 감안해도 넉넉하다» 고 적어뒀지만, 변경 전에는
        체류 5초가 rep 0 이었다 — 같은 원인(밴드 통과가 예산을 먹음)의 반대쪽 증상이다.
        """
        self.assertEqual(_count_reps(1.2, 5.0), 1)


class RestRejectionTests(unittest.TestCase):
    """#93 이 지키던 판별이 살아 있는가 — 이게 없으면 «고쳤다» 를 «상한을 없앴다» 와 못 가른다."""

    def test_sitting_rest_is_not_a_rep(self) -> None:
        for hold in (10.0, 30.0, 60.0, 90.0):
            with self.subTest(hold_sec=hold):
                self.assertEqual(
                    _count_reps(1.2, hold), 0, f"체류 {hold}s 는 휴식인데 rep 으로 셌다"
                )

    def test_slow_sit_down_is_still_rejected(self) -> None:
        """의자에 천천히 앉아 오래 쉬는 것 — 하강이 느려도 체류가 길면 여전히 거부된다."""
        self.assertEqual(_count_reps(5.0, 30.0), 0)


class DwellIsWhatIsMeasuredTests(unittest.TestCase):
    """상한이 재는 것이 «체류» 임을 직접 고정한다 — 위 두 클래스가 못 잡는 자리다.

    위 검사들은 rep 수만 본다. 그래서 예산 규칙이 바뀌어도 마침 결과가 같으면 통과한다.
    여기서는 카운터 자체를 읽어, 밴드 통과 시간이 예산에서 빠졌는지를 직접 본다.
    """

    def _bottom_count(self, descent: float, hold: float) -> int:
        state = SessionState(session_id=1, exercise_id=1)
        analyzer = StreamingSquatAnalyzer("squat")
        peak = 0

        total = _LEAD_SEC + descent + hold + descent + _TAIL_SEC
        for k in range(int(total * _FPS) + 1):
            analyzer.process_frame(
                state, _frame(_knee_angle_at(k / _FPS, descent, hold, descent))
            )
            peak = max(peak, state.bottom_frame_count)
        return peak

    def test_dwell_count_barely_moves_with_pace(self) -> None:
        """체류를 고정하고 하강 속도만 5배로 늘려도 카운터가 크게 안 는다.

        변경 전에는 이 값이 하강 속도에 정비례했다 — 하강 5초면 밴드 통과에만 4.70초를 썼다.
        완전히 0 이 되지는 않는다: 하강 궤적의 «100° 아래» 구간도 실제로 바닥 근처이므로
        일부는 정당하게 포함된다. 그래서 «같다» 가 아니라 «완만하다» 로 고정한다.
        """
        fast = self._bottom_count(1.2, 1.0)
        slow = self._bottom_count(6.0, 1.0)
        self.assertLessEqual(
            slow - fast,
            6,
            f"하강 1.2s→6.0s 에서 체류 카운트가 {fast}→{slow} 로 늘었다 — 속도 의존이 남았다",
        )

    def test_dwell_count_tracks_actual_hold(self) -> None:
        """반대로 체류를 늘리면 카운터는 따라 늘어야 한다 — 세는 대상이 맞는지 확인."""
        short = self._bottom_count(1.2, 0.5)
        long_ = self._bottom_count(1.2, 3.5)
        self.assertGreaterEqual(
            long_ - short,
            int(3.0 * _FPS) - 1,
            f"체류 0.5s→3.5s 인데 카운트가 {short}→{long_} 뿐이다 — 체류를 안 세고 있다",
        )


if __name__ == "__main__":
    unittest.main()