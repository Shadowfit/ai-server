"""프레임 시각의 origin 이 하나인가 (이슈 #156).

`timestamp_sec` 은 이름·계약서(`models/pose.py`)·엔티티 주석(`PoseData.java`)·리포트 포맷
(`SessionAnalysisCalculator.formatTimestamp`)이 전부 **「세션 시작 기준 경과 초」** 를 가정한다.
그런데 예전에는 그 컬럼에 서로 다른 origin 이 섞여 들어갔다:

    클라 (실시간)   Date.now() / 1000        → epoch. 리포트가 "29770991:08" 을 냈다
    fallback        float(state.frame_index) → 개수. 3fps 면 정확히 3배로 «그럴듯하게» 틀렸다

⚠️ 이슈 본문은 origin 이 **셋** 이라고 적었지만, 세 번째(배치 경로 `frame_idx / original_fps`)는
   `exercise_references.timestamp_sec` 이라는 **다른 테이블**로 간다. `pose_data.timestamp_sec` 에
   섞이던 것은 둘이다. 이 테스트가 덮는 범위도 그 둘이다.

이제 서버가 도착 시각(`time.monotonic`)으로 만든다. 아래 테스트가 그 성질을 고정한다.
"""
import unittest

from app.grpc.session_state import SessionState, elapsed_sec


class ElapsedOriginTests(unittest.TestCase):
    def test_first_frame_is_zero(self) -> None:
        """첫 프레임이 0 이다 — 리포트가 «운동 중 언제» 를 표시하기 때문이다.

        기준을 세션 «생성» 이 아니라 첫 프레임으로 잡는 이유: StartAnalysis 와 첫 프레임 사이에는
        사용자가 자세를 잡는 시간이 있고 그건 운동 시간이 아니다.
        """
        state = SessionState(session_id=1, exercise_id=1)
        self.assertEqual(elapsed_sec(state, 1000.0), 0.0)

    def test_elapsed_is_relative_not_absolute(self) -> None:
        """기준 시각이 아무리 커도 결과는 경과 초다 — monotonic 의 절대값이 새어나오면 안 된다.

        예전 결함이 정확히 이 형태였다: 절대 시각(epoch)이 그대로 흘러 «분» 자리가 폭발했다.
        `time.monotonic()` 도 부팅 후 경과라 절대값이 클 수 있으므로 같은 함정이 있다.
        """
        state = SessionState(session_id=1, exercise_id=1)
        elapsed_sec(state, 987_654.0)
        self.assertEqual(elapsed_sec(state, 987_654.0 + 75.0), 75.0)

    def test_client_supplied_value_is_ignored(self) -> None:
        """클라가 무엇을 보내든 결과가 안 바뀐다 — origin 이 서버에만 있다는 뜻이다.

        `SessionState` 는 클라 값을 담는 자리 자체가 없다. 이 테스트는 그 사실을 «없음» 으로
        확인한다 — 나중에 누가 클라 값을 되살리는 필드를 추가하면 여기서 드러난다.
        """
        self.assertNotIn(
            "client_timestamp",
            SessionState(session_id=1, exercise_id=1).__dict__,
            "클라 시각을 담는 상태가 생겼다 — origin 이 다시 둘이 된다",
        )

    def test_monotonic_input_never_goes_backwards(self) -> None:
        """같은 시각의 프레임 둘은 같은 값이다 — 단조성이 깨지지 않는다."""
        state = SessionState(session_id=1, exercise_id=1)
        elapsed_sec(state, 500.0)
        self.assertEqual(elapsed_sec(state, 530.0), 30.0)
        self.assertEqual(elapsed_sec(state, 530.0), 30.0)


class ReattachContinuityTests(unittest.TestCase):
    """재부착 후 시각이 이어지는가 — 이 안(ㄴ)에서 가장 깨지기 쉬운 자리다."""

    def test_offset_continues_the_timeline(self) -> None:
        """재부착 세션의 첫 프레임은 0 이 아니라 «이미 흐른 만큼» 이다.

        보정이 없으면 재부착 이후 프레임이 0 초부터 다시 시작해, 리포트의 최악 구간 시각이
        세션 앞부분과 겹치는 값으로 표시된다.
        """
        state = SessionState(session_id=1, exercise_id=1)
        state.elapsed_offset_sec = 120.0  # Spring 이 준 값 (ReattachRequest.elapsed_sec)

        self.assertEqual(elapsed_sec(state, 5_000.0), 120.0)
        self.assertEqual(elapsed_sec(state, 5_030.0), 150.0)

    def test_offset_is_not_applied_twice(self) -> None:
        """보정은 기준점을 옮기는 것이지 매 프레임 더해지는 값이 아니다."""
        state = SessionState(session_id=1, exercise_id=1)
        state.elapsed_offset_sec = 60.0

        values = [elapsed_sec(state, 100.0 + k) for k in range(4)]
        self.assertEqual(values, [60.0, 61.0, 62.0, 63.0])


if __name__ == "__main__":
    unittest.main()