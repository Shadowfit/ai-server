"""StreamingSquatAnalyzer 의 rep 집계 — 운동과 휴식을 가르는가 (이슈 #93).

상태머신은 무릎각만 보므로, 휴식 중 의자·바닥에 앉았다 일어나는 동작이 스쿼트와
같은 각도 궤적을 그린다. 그대로 두면 rep 카운트·sync_rate·리포트가 전부 부풀었다.

구분 축은 **바닥 체류 시간**이다. 하강 속도로는 갈리지 않는다 — 의자에 5초에 걸쳐
앉으면 프레임당 ~5.6° 라 descending 기준(delta <= -4)을 그냥 통과하고, 임계를 더
빡세게 잡으면 이번엔 천천히 하는 초보자의 정상 rep 이 걸린다. 반면 체류 시간은
스쿼트 ~1초 vs 앉아쉼 30~90초로 겹치지 않는다.

아래 테스트가 그 판별을 고정한다. 특히 `test_slow_squat_still_counts` 와
`test_sitting_rest_is_not_a_rep` 는 **같은 각도 범위를 오가고 체류 시간만 다르다** —
체류 상한을 없애거나 지나치게 키우면 후자가, 지나치게 줄이면 전자가 깨진다.
"""
import unittest

from app.core.squat_analyzer import StreamingSquatAnalyzer
from app.grpc.session_state import MAX_REP_FRAMES, PerRepFrame, SessionState

from tests.test_squat_analyzer import _frame


def _ramp(start: float, end: float, steps: int) -> list[float]:
    return [start + (end - start) * i / (steps - 1) for i in range(steps)]


def _count_reps(knee_sequence: list[float]) -> int:
    """무릎각 시퀀스를 흘려보내고 집계된 rep 수를 돌려준다.

    호출 순서는 pose.py 를 그대로 따른다 — process_frame 으로 판정한 뒤, 가시성을
    통과한 프레임만 버퍼에 넣고, rep 이 완성되면 버퍼를 비운다(pose.py:77·96·131).
    """
    state = SessionState(session_id=1, exercise_id=1)
    analyzer = StreamingSquatAnalyzer("squat")
    reps = 0

    for knee_angle in knee_sequence:
        angles, rep_event = analyzer.process_frame(state, _frame(knee_angle))
        if angles is not None:
            state.current_rep_frames.append(
                PerRepFrame(timestamp_sec=0.0, joint_coordinates="{}", angles=angles)
            )
        if rep_event is not None:
            reps += 1
            state.current_rep_frames.clear()

    return reps


# 3fps(프론트 intervalMs=330) 기준. 뒤에 서 있는 프레임을 두는 이유는 smoothing 이
# 최근 3프레임 평균이라 마지막 프레임에서 곧바로 standing 임계를 넘지 못해서다.
_STANDING_TAIL = [170.0] * 4


class StreamingRepCountingTests(unittest.TestCase):
    def test_normal_squat_counts_once(self) -> None:
        sequence = [170.0] * 3 + _ramp(170, 85, 5) + [85.0] + _ramp(85, 170, 5) + _STANDING_TAIL
        self.assertEqual(_count_reps(sequence), 1)

    def test_fast_squat_counts_once(self) -> None:
        """2초 스쿼트는 3fps 에서 5프레임뿐 — smoothing 이 극값을 뭉개도 집계돼야 한다."""
        sequence = [170.0] * 3 + [140.0, 95.0, 88.0, 95.0, 140.0] + _STANDING_TAIL
        self.assertEqual(_count_reps(sequence), 1)

    def test_slow_squat_still_counts(self) -> None:
        """8초짜리 아주 느린 스쿼트(초보자·재활). 하강·상승이 느릴 뿐 바닥 체류는 짧다."""
        sequence = (
            [170.0] * 3 + _ramp(170, 88, 12) + [88.0] * 2 + _ramp(88, 170, 12) + _STANDING_TAIL
        )
        self.assertEqual(_count_reps(sequence), 1)

    def test_sitting_rest_is_not_a_rep(self) -> None:
        """이슈 #93 회귀 — 의자에 앉아 30초 쉬고 일어나는 동작.

        무릎각 궤적만 보면 위 느린 스쿼트와 구분되지 않는다. 다른 것은 바닥에 머무는
        시간뿐이고, 그것이 이 판정의 근거다.
        """
        sequence = (
            [170.0] * 6
            + _ramp(170, 92, 15)     # 5초에 걸쳐 천천히 앉는다
            + [92.0] * 90            # 앉은 채 30초 휴식
            + _ramp(92, 170, 15)     # 5초에 걸쳐 일어난다
            + [170.0] * 6
        )
        self.assertEqual(_count_reps(sequence), 0)

    def test_standing_rest_is_not_a_rep(self) -> None:
        sequence = [170.0] * 6 + [168.0] * 90 + [170.0] * 6
        self.assertEqual(_count_reps(sequence), 0)

    def test_idle_frames_do_not_pile_into_next_rep(self) -> None:
        """이슈 #91 회귀 — 서서 오래 쉬어도 다음 rep 의 배치가 그만큼 부풀지 않는다.

        버퍼는 rep 완성 시에만 비워지므로, 상한이 없으면 서 있던 30초(90프레임)가 다음
        rep 배치에 통째로 실린다. 그 프레임들은 다음 rep 의 rep_number 를 달고 저장되고
        sync_rate 계산에도 섞인다.

        `bottom` 체류 상한(#93)은 <b>앉아서</b> 쉬는 경우만 비우므로 이 경로를 못 막는다 —
        서서 쉬면 `rep_state` 가 `ready` 그대로라 그 정리가 발동하지 않는다.
        """
        state = SessionState(session_id=1, exercise_id=1)
        analyzer = StreamingSquatAnalyzer("squat")
        idle_frames = 90  # 3fps 기준 30초

        sequence = [170.0] * idle_frames + _ramp(170, 85, 5) + [85.0] + _ramp(85, 170, 5) + _STANDING_TAIL
        batch_sizes = []

        for knee_angle in sequence:
            angles, rep_event = analyzer.process_frame(state, _frame(knee_angle))
            if angles is not None:
                state.current_rep_frames.append(
                    PerRepFrame(timestamp_sec=0.0, joint_coordinates="{}", angles=angles)
                )
            if rep_event is not None:
                batch_sizes.append(len(state.current_rep_frames))
                state.current_rep_frames.clear()

        self.assertEqual(len(batch_sizes), 1, "rep 은 1회 집계돼야 한다")
        self.assertLessEqual(
            batch_sizes[0], MAX_REP_FRAMES,
            f"배치가 상한을 넘었다 — 유휴 프레임 {idle_frames}개가 실려 나갔다",
        )
        self.assertLess(
            batch_sizes[0], idle_frames,
            "상한이 걸리지 않아 유휴 구간이 그대로 배치에 남았다",
        )

    def test_real_squat_after_sitting_rest_still_counts(self) -> None:
        """휴식을 걸러낸 뒤에도 다음 세트의 첫 rep 을 정상 집계해야 한다.

        휴식 구간에서 상태를 ready 로 되돌리지 않으면 bottom 에 갇혀 이후 rep 을 통째로
        놓친다 — 걸러내기가 만들 수 있는 가장 나쁜 부작용이라 따로 고정한다.
        """
        rest = [170.0] * 6 + _ramp(170, 92, 15) + [92.0] * 90 + _ramp(92, 170, 15) + [170.0] * 6
        squat = _ramp(170, 85, 5) + [85.0] + _ramp(85, 170, 5) + _STANDING_TAIL
        self.assertEqual(_count_reps(rest + squat), 1)


if __name__ == "__main__":
    unittest.main()
