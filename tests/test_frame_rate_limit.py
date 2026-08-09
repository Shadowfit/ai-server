"""유입 속도 상한이 fps 상승으로 사라지던 rep 을 되살리는가 (이슈 #143 · ㄱ-2 안).

#143 의 결함: rep 판정 상수 3개가 «프레임 개수» 로 시간을 인코딩하는데 개수를 초로 되돌리는
fps 가 코드로 고정돼 있지 않다. 이슈 코멘트의 측정이 결과를 고정해뒀다 — **10fps 에서 바닥
체류 0.5초짜리 정상 스쿼트가 rep 0 으로 사라진다** (3fps 에서는 체류 4.3초까지 버틴다).

이 테스트는 그 재현을 **리미터와 함께** 돌린다. 아래 `_count_reps` 는 #143 코멘트가 쓴 하니스와
같은 궤적·같은 호출 순서를 쓰되, `process_frame` 앞에 `accept_frame` 을 끼운다. 즉 통과하면
"고쳐졌다" 가 아니라 **"측정으로 확인된 그 실패가 이 변경으로 사라진다"** 는 뜻이다.

⚠️ 궤적은 **합성**이다 (하강 1.2s 선형 · 상승 1.2s 선형). #143 의 원 측정과 같은 한계를
그대로 물려받는다 — 실제 스쿼트 각도 로그로 재확인한 것이 아니다.

⚠️ 상한(3.33fps)과 현재 클라(3.03fps)가 **정확히 같지는 않다.** 그래서 10fps 클라를 깎은
스트림은 native 3fps 보다 프레임이 약간 더 촘촘하고, bottom 예산도 5.0초가 아니라 4.5초다.
`test_hold_boundary_is_no_longer_fps_dependent` 가 그 잔차를 수치로 고정한다.
"""
import unittest

from app.core.squat_analyzer import StreamingSquatAnalyzer
from app.grpc.session_state import (
    MIN_FRAME_INTERVAL_SEC,
    PerRepFrame,
    SessionState,
    accept_frame,
)

from tests.test_squat_analyzer import _frame

# #143 코멘트가 쓴 «시간으로 정의한 스쿼트 1회». fps 는 이 궤적을 샘플링하는 간격일 뿐이고,
# 사람의 동작 자체는 fps 와 무관하게 같다 — 그게 이 재현의 핵심이다.
_STAND_LEAD_SEC = 1.0
_DESCENT_SEC = 1.2
_ASCENT_SEC = 1.2
_STAND_TAIL_SEC = 1.5

_STANDING_ANGLE = 170.0
_BOTTOM_ANGLE = 85.0


def _knee_angle_at(t: float, hold_sec: float) -> float:
    """서있기 → 하강 → 바닥 체류 → 상승 → 서있기 궤적의 시각 t 에서의 무릎각."""
    descent_start = _STAND_LEAD_SEC
    hold_start = descent_start + _DESCENT_SEC
    ascent_start = hold_start + hold_sec
    ascent_end = ascent_start + _ASCENT_SEC

    if t < descent_start:
        return _STANDING_ANGLE
    if t < hold_start:
        ratio = (t - descent_start) / _DESCENT_SEC
        return _STANDING_ANGLE + (_BOTTOM_ANGLE - _STANDING_ANGLE) * ratio
    if t < ascent_start:
        return _BOTTOM_ANGLE
    if t < ascent_end:
        ratio = (t - ascent_start) / _ASCENT_SEC
        return _BOTTOM_ANGLE + (_STANDING_ANGLE - _BOTTOM_ANGLE) * ratio
    return _STANDING_ANGLE


def _total_sec(hold_sec: float) -> float:
    return _STAND_LEAD_SEC + _DESCENT_SEC + hold_sec + _ASCENT_SEC + _STAND_TAIL_SEC


def _count_reps(fps: float, hold_sec: float, *, limiter: bool) -> int:
    """궤적을 fps 로 샘플링해 흘려보내고 집계된 rep 수를 돌려준다.

    호출 순서는 pose.py 를 그대로 따른다 — 상한 판정 → process_frame → 버퍼 적재 → rep 완성 시
    버퍼 비움. `limiter=False` 면 상한 없이 돌아가고, 그건 곧 **변경 전 동작**이다.
    """
    state = SessionState(session_id=1, exercise_id=1)
    analyzer = StreamingSquatAnalyzer("squat")
    reps = 0

    sample_count = int(_total_sec(hold_sec) * fps) + 1
    for k in range(sample_count):
        now = k / fps
        if limiter and not accept_frame(state, now):
            continue

        angles, _, rep_event = analyzer.process_frame(
            state, _frame(_knee_angle_at(now, hold_sec))
        )
        if angles is not None:
            state.current_rep_frames.append(
                PerRepFrame(timestamp_sec=now, joint_coordinates="{}", angles=angles)
            )
        if rep_event is not None:
            reps += 1
            state.current_rep_frames.clear()

    return reps


class FrameRateLimitReproductionTests(unittest.TestCase):
    """#143 의 측정된 실패가 상한으로 사라지는가."""

    # #143 코멘트의 표 그대로 — 기대값은 전부 1 이고, 변경 전에는 10·30fps 에서 0 이었다.
    FPS_CASES = (3.0, 10.0, 30.0)
    HOLD_CASES = (0.5, 1.0, 1.5, 2.0, 3.0)

    def test_without_limiter_reproduces_the_defect(self) -> None:
        """상한이 없으면 10·30fps 에서 rep 이 사라진다 — 이 테스트가 헛돌지 않는다는 증거."""
        vanished = [
            (fps, hold)
            for fps in (10.0, 30.0)
            for hold in self.HOLD_CASES
            if _count_reps(fps, hold, limiter=False) == 0
        ]
        self.assertEqual(
            len(vanished),
            len(self.HOLD_CASES) * 2,
            f"#143 재현이 안 됐다 — 상한 없이도 살아남은 조합이 있다: {vanished}",
        )

    def test_limiter_restores_reps_at_every_fps(self) -> None:
        """상한이 있으면 fps 가 얼마든 정상 스쿼트 1회는 1회로 집계된다."""
        for fps in self.FPS_CASES:
            for hold in self.HOLD_CASES:
                with self.subTest(fps=fps, hold_sec=hold):
                    self.assertEqual(
                        _count_reps(fps, hold, limiter=True),
                        1,
                        f"{fps}fps · 체류 {hold}s 에서 정상 rep 이 집계되지 않았다",
                    )

    def test_current_client_behaviour_is_unchanged(self) -> None:
        """현재 클라(3fps)는 상한을 안 탄다 — 이 변경은 오늘 동작을 바꾸지 않는다.

        상한(300ms)이 클라 간격(330ms)보다 짧아서 드롭이 0 이어야 하고, 결과도 상한 전후가
        같아야 한다. 이게 깨지면 «잠복 결함을 고치려다 활성 동작을 건드린» 것이다.
        """
        for hold in self.HOLD_CASES:
            with self.subTest(hold_sec=hold):
                self.assertEqual(
                    _count_reps(3.0, hold, limiter=True),
                    _count_reps(3.0, hold, limiter=False),
                )

    def test_sitting_rest_is_still_rejected(self) -> None:
        """상한을 넣어도 «앉아서 쉬는 것» 은 여전히 rep 이 아니다 (#93 가 지키던 판별).

        상한은 fps 를 묶을 뿐 체류 상한을 없애지 않는다. 이 검사가 없으면 "rep 이 살아났다" 를
        "상한이 무력화됐다" 와 구분하지 못한다.
        """
        for fps in self.FPS_CASES:
            with self.subTest(fps=fps):
                self.assertEqual(_count_reps(fps, 30.0, limiter=True), 0)


class FrameRateLimitMechanismTests(unittest.TestCase):
    """리미터 자체의 동작 — 지터 흡수와 크레딧 처리."""

    def _accepted(self, arrival_times: list[float]) -> int:
        state = SessionState(session_id=1, exercise_id=1)
        for now in arrival_times:
            accept_frame(state, now)
        return state.accepted_frame_count

    def test_on_contract_client_is_not_throttled(self) -> None:
        """규약대로 330ms 로 보내는 클라는 드롭되지 않는다."""
        arrivals = [0.330 * k for k in range(100)]
        self.assertEqual(self._accepted(arrivals), 100)

    def test_deadline_carry_absorbs_jitter(self) -> None:
        """평균은 규약을 지키고 개별 간격만 흔들리는 클라의 프레임을 지켜내는가.

        데드라인을 **«수락 시각 + 간격»** 으로 밀면 늦게 온 프레임이 기준을 그만큼 뒤로 밀고,
        다음 프레임은 그 밀린 기준과 비교되므로 지터가 누적된다. **«직전 데드라인 + 간격»** 으로
        미는 지금 방식은 장기 평균만 제한한다. 이 검사가 그 선택을 고정한다 — 두 방식을 같은
        도착열에 돌려 비교하므로, 구현이 나이브 쪽으로 바뀌면 즉시 깨진다.

        ⚠️ 드롭이 0 이 되는 게 정답은 **아니다.** 아래 톱니는 실제로 280ms 간격을 20번 만든다
        (상한 300ms 미만 = 클라가 그 순간 규약보다 빨리 보낸 것). 그걸 자르는 것이 이 상한의
        일이다. 여기서 고정하는 것은 «자르지 않는 것» 이 아니라 «누적되지 않는 것» 이다.
        """
        # 결정론적인 톱니 지터 (±25ms). 난수를 쓰면 실패가 재현되지 않는다.
        jitter = [0.025, -0.025, 0.015, -0.015, 0.0]
        arrivals = [0.330 * k + jitter[k % len(jitter)] for k in range(100)]

        # 나이브 방식 — 데드라인을 «수락 시각» 에서 민다.
        naive_accepted = 0
        last_accepted: float | None = None
        for now in arrivals:
            if last_accepted is None or now - last_accepted >= MIN_FRAME_INTERVAL_SEC:
                naive_accepted += 1
                last_accepted = now

        accepted = self._accepted(arrivals)

        self.assertGreater(
            accepted,
            naive_accepted,
            f"이월이 지터를 흡수하지 못했다 (이월 {accepted} vs 나이브 {naive_accepted})",
        )
        # 도착 간격이 상한을 밑돈 횟수는 20 이다. 그보다 한참 적게 잘려야 «누적이 아니다».
        self.assertLessEqual(
            100 - accepted,
            10,
            f"{100 - accepted}장이 버려졌다 — 상한 미달 구간 20회 대비 누적 드롭에 가깝다",
        )

    def test_fast_client_is_throttled_to_the_cap(self) -> None:
        """30fps 클라는 상한(3.33fps) 근처로 깎인다."""
        duration = 10.0
        arrivals = [k / 30.0 for k in range(int(duration * 30))]
        accepted = self._accepted(arrivals)
        expected = duration / MIN_FRAME_INTERVAL_SEC
        self.assertAlmostEqual(accepted, expected, delta=2)

    def test_idle_credit_is_not_banked(self) -> None:
        """느리게 보내다 갑자기 몰아쳐도 통과하는 것은 한 장뿐이다.

        데드라인 이월을 클램프하지 않으면, 쉬는 동안 쌓인 «크레딧» 만큼이 재개 직후 한꺼번에
        통과한다. 그게 정확히 이 상한이 막으려던 상황이다.
        """
        idle = [1.0 * k for k in range(20)]  # 20초간 1fps — 상한보다 한참 느리다
        burst = [20.0 + k / 30.0 for k in range(30)]  # 그 직후 30fps 로 1초간
        state = SessionState(session_id=1, exercise_id=1)
        for now in idle:
            accept_frame(state, now)
        before = state.accepted_frame_count
        for now in burst:
            accept_frame(state, now)

        burst_accepted = state.accepted_frame_count - before
        self.assertLessEqual(
            burst_accepted, 4, f"버스트에서 {burst_accepted}장이 통과했다 — 크레딧이 쌓였다"
        )

    def test_counters_track_both_sides(self) -> None:
        """수락·드롭 카운터가 실제 유입을 다 센다 — 드롭률 로그의 분모가 된다."""
        state = SessionState(session_id=1, exercise_id=1)
        arrivals = [k / 30.0 for k in range(30)]
        for now in arrivals:
            accept_frame(state, now)
        self.assertEqual(
            state.accepted_frame_count + state.dropped_frame_count, len(arrivals)
        )
        self.assertGreater(state.dropped_frame_count, 0)


class HoldBoundaryTests(unittest.TestCase):
    """체류 임계가 fps 에 얼마나 덜 의존하게 됐나 — 잔차를 수치로 남긴다."""

    def _boundary(self, fps: float) -> float:
        """rep 이 사라지기 시작하는 바닥 체류 시간 (0.1초 해상도)."""
        hold = 0.1
        while hold < 10.0:
            if _count_reps(fps, round(hold, 1), limiter=True) == 0:
                return round(hold, 1)
            hold += 0.1
        return 10.0

    def test_hold_boundary_is_no_longer_fps_dependent(self) -> None:
        """변경 전 3fps 4.3s / 10fps 0.5s / 30fps 0.2s → 변경 후 셋이 한 자리로 모인다.

        완전히 같아지지는 않는다. 상한은 3.33fps 이고 native 클라는 3.03fps 라, 깎인 스트림이
        약간 더 촘촘하고 예산(4.5s)도 native(5.0s)보다 짧다. 그 잔차를 «같다» 로 적지 않고
        범위로 고정한다.
        """
        boundaries = {fps: self._boundary(fps) for fps in (3.0, 10.0, 30.0)}

        for fps, boundary in boundaries.items():
            with self.subTest(fps=fps):
                self.assertGreaterEqual(
                    boundary,
                    3.0,
                    f"{fps}fps 의 체류 임계가 {boundary}s — 실제 체류 ~1초 대비 여유가 없다 "
                    f"(전체: {boundaries})",
                )

        spread = max(boundaries.values()) - min(boundaries.values())
        self.assertLessEqual(
            spread, 1.0, f"fps 별 체류 임계가 여전히 {spread}s 벌어진다: {boundaries}"
        )


if __name__ == "__main__":
    unittest.main()