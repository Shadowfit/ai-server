"""스트리밍 경로가 프레임별 깊이 지표를 실제로 만들어 내보내는지 검증.

이 경로에는 그동안 테스트가 없었다. 그래서 StreamingRepEvent 의 deepest_knee_angle ·
mean_torso_tilt 가 **한 번도 소비되지 않는 값**인 채로 결함 2건을 안고 남아 있었다(이슈 #85):
평균이 아닌 mean_torso_tilt, 왼쪽 무릎만 보던 deepest_knee_angle. 소비처가 없으니 테스트도
붙지 않았고, 테스트가 없으니 결함이 드러나지 않았다.

여기서 고정하는 것은 "값이 계산된다"가 아니라 **"그 값이 Spring 까지 갈 수 있는 모양으로 나온다"**
이다 — 깊이 지표는 rep 안에서 프레임마다 달라야 의미가 있고(sync_rate 는 rep 상수라 못 쓴다),
rep 경계를 판정하는 값과 같은 정의여야 "이 rep 의 바닥"과 "가장 깊은 프레임"이 어긋나지 않는다.
(docs/decisions/worst-section-rep-resolution.md §4-ㄹ)
"""

import math
import unittest

from app.core.squat_analyzer import StreamingSquatAnalyzer
from app.grpc.session_state import PerRepFrame, SessionState
from app.models.pose import Landmark


def _landmark(index: int, x: float, y: float, visibility: float = 0.99) -> Landmark:
    return Landmark(index=index, x=x, y=y, z=0.0, visibility=visibility)


def _frame(knee_angle: float, torso_tilt: float = 5.0) -> list[Landmark]:
    """무릎각이 knee_angle 인 프레임. test_squat_analyzer 의 헬퍼와 같은 기하다."""
    landmarks = [_landmark(index, 0.5, 0.5, visibility=0.1) for index in range(33)]
    knee_center = (0.5, 0.68)
    ankle_center = (0.5, 0.88)

    knee_rad = math.radians(180.0 - knee_angle)
    hip_center = (
        knee_center[0] - 0.2 * math.sin(knee_rad),
        knee_center[1] - 0.2 * math.cos(knee_rad),
    )
    torso_rad = math.radians(torso_tilt)
    shoulder_center = (
        hip_center[0] + 0.16 * math.sin(torso_rad),
        hip_center[1] - 0.16 * math.cos(torso_rad),
    )

    coordinates = {
        11: (shoulder_center[0] - 0.03, shoulder_center[1]),
        12: (shoulder_center[0] + 0.03, shoulder_center[1]),
        23: (hip_center[0] - 0.03, hip_center[1]),
        24: (hip_center[0] + 0.03, hip_center[1]),
        25: (knee_center[0] - 0.03, knee_center[1]),
        26: (knee_center[0] + 0.03, knee_center[1]),
        27: (ankle_center[0] - 0.03, ankle_center[1]),
        28: (ankle_center[0] + 0.03, ankle_center[1]),
    }
    for index, (x, y) in coordinates.items():
        landmarks[index] = _landmark(index, x, y)
    return landmarks


def _state() -> SessionState:
    return SessionState(session_id=1, exercise_id=1, reference_angles=[])


class StreamingDepthMetricTests(unittest.TestCase):
    def test_process_frame_returns_smoothed_knee_angle(self) -> None:
        """process_frame 이 깊이 지표를 반환값으로 내보낸다."""
        analyzer = StreamingSquatAnalyzer()
        state = _state()

        angles, smoothed, rep_event = analyzer.process_frame(state, _frame(170))

        self.assertIsNotNone(angles)
        self.assertIsNotNone(smoothed)
        self.assertIsNone(rep_event)  # 1프레임으로는 rep 이 완성되지 않는다
        self.assertGreater(smoothed, 0.0)

    def test_visibility_skip_returns_none_for_all_three(self) -> None:
        """가시성 미달 프레임은 세 값 모두 None — 호출자가 프레임을 버릴 수 있어야 한다."""
        analyzer = StreamingSquatAnalyzer()
        state = _state()
        invisible = [_landmark(i, 0.5, 0.5, visibility=0.1) for i in range(33)]

        angles, smoothed, rep_event = analyzer.process_frame(state, invisible)

        self.assertIsNone(angles)
        self.assertIsNone(smoothed)
        self.assertIsNone(rep_event)

    def test_depth_metric_varies_within_a_rep(self) -> None:
        """★ 핵심 — 깊이 지표는 rep 안에서 프레임마다 다르다.

        sync_rate 는 rep 단위로 채점돼 프레임마다 복제되므로 rep 안에서 상수다. 그래서 "이 rep 의
        어느 순간이 바닥이었나"를 sync_rate 로는 답할 수 없다. 이 값이 그 자리를 메운다 — 값이
        변하지 않으면 이 안 전체가 성립하지 않으므로 여기서 고정한다.
        """
        analyzer = StreamingSquatAnalyzer()
        state = _state()
        descent = [175, 160, 130, 110, 90, 85]

        measured = []
        for knee_angle in descent:
            _, smoothed, _ = analyzer.process_frame(state, _frame(knee_angle))
            measured.append(smoothed)

        self.assertEqual(len(set(measured)), len(measured), "프레임마다 값이 달라야 한다")
        # 내려가는 구간이므로 단조 감소여야 한다(평활 때문에 실제 각도보다 완만하다)
        self.assertTrue(all(b < a for a, b in zip(measured, measured[1:])))

    def test_depth_metric_is_smoothed_not_raw(self) -> None:
        """평활값이다 — 한 프레임이 튀어도 그 프레임이 곧바로 최소값이 되지 않는다.

        원시값을 쓰면 랜드마크 추정이 한 프레임 흔들릴 때 그 프레임이 대표로 뽑혀 리포트에
        이상한 뼈대가 그려진다. 평활은 그걸 막는 대신 최소점을 1~2프레임 밀리게 한다.
        """
        analyzer = StreamingSquatAnalyzer()
        state = _state()

        for knee_angle in (170, 170):
            analyzer.process_frame(state, _frame(knee_angle))
        _, smoothed, _ = analyzer.process_frame(state, _frame(80))  # 한 프레임만 급락

        # 원시값이면 ~80 이 나와야 하지만 최근 3프레임 평균이라 훨씬 완만하다
        self.assertGreater(smoothed, 100.0)

    def test_depth_metric_uses_both_knees(self) -> None:
        """좌우 평균이다 — 한쪽 다리만 굽힌 순간이 '가장 깊은 순간'으로 뽑히지 않는다.

        rep 경계를 판정하는 값이 좌우 평균이므로(_extract_raw_metrics) 정의를 맞춘 것이다.
        왼쪽 무릎만 보면 "왜 왼쪽인가"에 답이 없고, 실제로 그게 이슈 #85 의 결함 2번이었다.
        """
        analyzer = StreamingSquatAnalyzer()

        symmetric = _frame(120)

        # 오른쪽 무릎각만 바꾼 프레임 — 왼쪽(25/27)은 건드리지 않는다.
        # 발목을 아래로 내리면 무릎-발목이 그대로 일직선이라 각도가 안 바뀐다. x 로 옮겨야 한다.
        asymmetric = _frame(120)
        right_ankle = asymmetric[28]
        asymmetric[28] = _landmark(28, right_ankle.x + 0.12, right_ankle.y)

        _, symmetric_value, _ = analyzer.process_frame(_state(), symmetric)
        _, asymmetric_value, _ = analyzer.process_frame(_state(), asymmetric)

        self.assertNotAlmostEqual(
            symmetric_value,
            asymmetric_value,
            places=1,
            msg="오른쪽 무릎이 달라졌는데 값이 같다면 왼쪽만 보고 있는 것이다",
        )

    def test_rep_event_carries_no_unconsumed_summary(self) -> None:
        """StreamingRepEvent 에 소비되지 않는 rep 요약 필드가 다시 생기지 않게 고정 (#85).

        deepest_knee_angle · mean_torso_tilt 는 배치 경로에서 이름째 가져온 뒤 스트리밍에서는
        한 번도 읽히지 않았고, 그 상태로 결함 2건을 안고 있었다. 프레임별 값을 Spring 이 직접
        받으므로 rep 요약본은 필요 없다 — 같은 사실을 두 곳에 저장하지 않는다.
        """
        from app.core.squat_analyzer import StreamingRepEvent

        fields = set(StreamingRepEvent.__dataclass_fields__)

        self.assertEqual(fields, {"rep_number", "sync_rate", "feedback_message"})

    def test_per_rep_frame_carries_depth_metric(self) -> None:
        """PerRepFrame 이 깊이 지표를 실어야 콜백에서 프레임별로 꺼낼 수 있다."""
        frame = PerRepFrame(
            timestamp_sec=1.5,
            joint_coordinates="{}",
            angles=[100.0, 102.0, 80.0, 82.0],
            smoothed_knee_angle=101.0,
        )

        self.assertEqual(frame.smoothed_knee_angle, 101.0)
        # 기본값 0.0 = 미상. Spring 이 이 값을 후보에서 제외하고 예전 동작으로 떨어진다.
        self.assertEqual(
            PerRepFrame(timestamp_sec=0.0, joint_coordinates="{}", angles=[]).smoothed_knee_angle,
            0.0,
        )
