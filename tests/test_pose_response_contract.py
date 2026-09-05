"""`POST /api/v1/pose` 의 응답이 **사실을 말하는가** (이슈 #267).

#196 통주행이 「HTTP 전 구간 200 인데 리포트 전 필드 0」을 정상으로 읽었다. 원인은 status 가
아니라 **`success` 축이 실제 상태를 안 담고 있었다는 것**이다 — 판정에 못 들어간 프레임도
`success=true` 에 `landmarks` 까지 채워 나갔고, 그래서 「검출 30/31」로 세면 멀쩡해 보였다.
`e1_walkthrough.py` 도 같은 자리에 한 번 걸렸다(`edb91cf`).

그래서 `success` 를 **«판정에 들어갔는가»** 로 좁히고 `skip_reason` 을 별도 축으로 뒀다.
이 파일이 고정하는 것은 그 계약이다.

⚠️ **여기서 재는 것은 응답 계약이지 검출 품질이 아니다.** MediaPipe 는 mock 으로 갈음한다 —
「가시성이 낮은 프레임을 실제로 알아보는가」는 `test_squat_analyzer` 쪽 몫이다.
"""
import unittest
from unittest import mock

import numpy as np

from app.api.endpoints import pose as pose_endpoint
from app.grpc.session_state import SessionState, get_registry
from app.models.pose import PoseRequest, PoseSkipReason

from tests.test_squat_analyzer import _frame

_BLANK_IMAGE = np.zeros((4, 4, 3), dtype=np.uint8)
_STANDING_ANGLE = 172.0

# 33개 랜드마크 전부 visibility 0.1 — `_frame` 이 하체만 올려주므로, 그걸 안 쓰면
# `_frame_visibility_score` 가 바닥이라 «가시성 부족» 갈래로 떨어진다.
_INVISIBLE = [
    type(lm)(index=lm.index, x=lm.x, y=lm.y, z=lm.z, visibility=0.01)
    for lm in _frame(_STANDING_ANGLE)
]


def _fake_lease(detect_fn):
    class _L:
        def __enter__(self):
            return mock.Mock(detect=detect_fn)

        def __exit__(self, *exc):
            return False

    return lambda _session_id: _L()


class PoseResponseContractTest(unittest.TestCase):
    def _run(self, session_id, detect_fn, *, lease=True):
        req = PoseRequest(image="", session_id=session_id, exercise_type="squat")
        lease_patch = _fake_lease(detect_fn) if lease else (lambda _s: None)
        with mock.patch.object(pose_endpoint, "base64_to_image", lambda _: _BLANK_IMAGE), \
            mock.patch.object(pose_endpoint, "lease_detector", lease_patch):
            return pose_endpoint.detect_pose(req)

    def _session(self, session_id):
        state = get_registry().create(
            session_id=session_id, exercise_id=1, reference_angles=[]
        )
        self.addCleanup(get_registry().remove, session_id)
        return state

    # ── 판정에 들어간 프레임 ────────────────────────────────────────────────

    def test_판정에_들어간_프레임은_success_True_에_사유가_없다(self) -> None:
        self._session(9201)
        res = self._run(9201, lambda _img: _frame(_STANDING_ANGLE))

        self.assertTrue(res.success)
        self.assertIsNone(res.skip_reason, "성공 응답에 스킵 사유가 붙어 있다")
        self.assertIsNotNone(res.angles, "판정에 들어갔다면서 각도가 없다")

    # ── #267 의 두 자리 ────────────────────────────────────────────────────

    def test_가시성_부족은_success_False_다(self) -> None:
        """#196 이 속은 바로 그 자리. landmarks 는 오는데 판정은 0 이다."""
        state = self._session(9202)
        res = self._run(9202, lambda _img: _INVISIBLE)

        self.assertFalse(res.success, "판정에 못 들어갔는데 success=true 다 (#267 재발)")
        self.assertEqual(res.skip_reason, PoseSkipReason.LOW_VISIBILITY)
        self.assertIsNone(res.angles)
        # 🔴 오독의 원인이 여기다 — landmarks 는 **그대로 있다.**
        self.assertIsNotNone(
            res.landmarks,
            "오버레이용 landmarks 까지 지우면 화면이 끊긴다 — 막는 것은 판정이지 표시가 아니다",
        )
        self.assertEqual(state.visibility_skip_count, 1, "세션 요약이 셀 근거가 안 쌓인다")

    def test_유입_상한_드롭도_success_False_다(self) -> None:
        """서버가 «의도적으로» 자른 경우다. 그래도 판정에는 안 들어갔다."""
        self._session(9203)
        with mock.patch.object(pose_endpoint, "accept_frame", lambda _s, _n: False):
            res = self._run(9203, lambda _img: _frame(_STANDING_ANGLE))

        self.assertFalse(res.success)
        self.assertEqual(res.skip_reason, PoseSkipReason.RATE_LIMITED)
        self.assertIsNotNone(res.landmarks)

    def test_정상_드롭과_입력_문제는_사유로_갈린다(self) -> None:
        """`success` 한 축으로는 못 가르는 것 — skip_reason 이 존재하는 이유다."""
        self._session(9204)
        with mock.patch.object(pose_endpoint, "accept_frame", lambda _s, _n: False):
            dropped = self._run(9204, lambda _img: _frame(_STANDING_ANGLE))
        invisible = self._run(9204, lambda _img: _INVISIBLE)

        self.assertEqual(dropped.success, invisible.success, "전제: 둘 다 False 다")
        self.assertNotEqual(
            dropped.skip_reason,
            invisible.skip_reason,
            "정상 동작(상한)과 입력 문제(가시성)가 같은 사유로 나온다",
        )

    # ── 나머지 거절 갈래도 사유를 단다 ──────────────────────────────────────

    def test_배정_없음도_세션_없음과_구분되면_안_된다(self) -> None:
        """#605 — lease 게이트가 registry 게이트보다 먼저 돈다고 응답이 갈리면,
        session_id 를 훑어 "지금 배정된 분석기가 있는 세션"(≈ 진행 중)을 열거할 수 있다.
        #187 안 (d)가 정한 "거절은 세션 없음과 같은 모양이어야 한다"를 첫 게이트에도 적용한다.
        """
        self._session(9205)
        no_lease = self._run(9205, None, lease=False)

        self.assertFalse(no_lease.success)
        self.assertEqual(no_lease.skip_reason, PoseSkipReason.SESSION_NOT_FOUND)
        self.assertEqual(
            no_lease.message,
            "세션 9205가 시작되지 않았습니다 (StartAnalysis 먼저 호출 필요)",
            "같은 session_id 라면 registry 게이트가 내는 메시지와 글자 하나까지 같아야 한다",
        )

    def test_세션_미시작은_SESSION_NOT_FOUND_다(self) -> None:
        # 레지스트리에 안 만든다.
        res = self._run(9206, lambda _img: _frame(_STANDING_ANGLE))

        self.assertFalse(res.success)
        self.assertEqual(res.skip_reason, PoseSkipReason.SESSION_NOT_FOUND)

    def test_포즈_미검출은_NO_POSE_다(self) -> None:
        self._session(9207)
        res = self._run(9207, lambda _img: [])

        self.assertFalse(res.success)
        self.assertEqual(res.skip_reason, PoseSkipReason.NO_POSE)

    # ── 계약 자체 ──────────────────────────────────────────────────────────

    def test_success_와_skip_reason_은_같이_움직인다(self) -> None:
        """둘이 어긋나면 축을 둘로 나눈 의미가 없다.

        🔴 **세션을 갈라 쓴다.** 처음에는 한 세션에 연달아 두 번 넣었는데, 두 번째가 유입
        상한(300ms)에 걸려 `LOW_VISIBILITY` 가 아니라 `RATE_LIMITED` 를 받고 있었다 —
        단언은 그대로 통과해서 **가시성 갈래가 조용히 안 덮였다**(2026-08-20 리뷰 지적).
        """
        self._session(9208)
        self._session(9210)
        cases = [
            self._run(9208, lambda _img: _frame(_STANDING_ANGLE)),  # 판정됨
            self._run(9210, lambda _img: _INVISIBLE),               # 가시성 부족
            self._run(9209, lambda _img: _frame(_STANDING_ANGLE)),  # 세션 없음
        ]
        # 의도한 갈래를 실제로 밟았는지부터 고정한다 — 이게 없으면 위 사고가 또 조용히 난다.
        self.assertEqual(
            [c.skip_reason for c in cases],
            [None, PoseSkipReason.LOW_VISIBILITY, PoseSkipReason.SESSION_NOT_FOUND],
            "케이스가 의도한 갈래를 안 밟았다",
        )
        for res in cases:
            self.assertEqual(
                res.success,
                res.skip_reason is None,
                f"success={res.success} 인데 skip_reason={res.skip_reason} 이다",
            )


class FrameIntakeDiagnosticTest(unittest.TestCase):
    """세션 종료 때 「판정 0」을 알아보는가 (#267 곁가지).

    StopAnalysis 자체는 gRPC 스텁이 필요해 이 저장소가 단위 테스트하지 않는다
    (`test_stop_idempotency` 머리말). 그래서 **판단만** 상태 속성으로 빼서 여기서 고정한다 —
    로그 문구는 servicer 에 남고, 「무엇을 경고할 것인가」는 이 테스트가 지킨다.
    """

    def _state(self, *, accepted=0, dropped=0, visibility=0):
        state = SessionState(session_id=1, exercise_id=1)
        state.accepted_frame_count = accepted
        state.dropped_frame_count = dropped
        state.visibility_skip_count = visibility
        return state

    def test_사람을_아예_못_찾은_세션도_경고한다(self) -> None:
        """🔴 리뷰가 잡은 자리 (2026-08-20).

        NO_POSE 는 `accept_frame` **앞에서** 반환하므로 카운터가 전부 0 이다. 처음 판에서는
        경고를 `visibility_skip_count` 로 걸어서, 정작 이 경우 — 리포트가 전 필드 0 으로
        끝나는 #196 그 상태 — 에 StopAnalysis 가 아무 말도 안 했다.
        """
        state = self._state()  # 수락 0 · 드롭 0 · 가시성 0

        self.assertEqual(state.judged_frame_count, 0)
        self.assertTrue(
            state.needs_intake_warning,
            "카운터가 전부 0 인 세션이 조용히 끝난다 — 이게 제일 나쁜 경우다",
        )

    def test_가시성으로_전부_떨어진_세션을_경고한다(self) -> None:
        state = self._state(accepted=30, visibility=30)

        self.assertEqual(state.judged_frame_count, 0)
        self.assertTrue(state.needs_intake_warning)

    def test_일부만_떨어져도_경고한다(self) -> None:
        """판정이 살아 있어도 스킵이 있으면 알려준다 — 조용한 손실을 막는 쪽이다."""
        state = self._state(accepted=30, visibility=10)

        self.assertEqual(state.judged_frame_count, 20)
        self.assertTrue(state.needs_intake_warning)

    def test_건강한_세션은_조용하다(self) -> None:
        """상한 드롭만 있는 것은 정상 동작이라 이 경고의 대상이 아니다(#143 로그가 따로 있다)."""
        state = self._state(accepted=30, dropped=12)

        self.assertEqual(state.judged_frame_count, 30)
        self.assertFalse(state.needs_intake_warning)


if __name__ == "__main__":
    unittest.main()
