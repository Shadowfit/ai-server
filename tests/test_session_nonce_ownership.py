"""세션 소유권 대조 — 남의 session_id 로 프레임을 꽂을 수 있는가 (이슈 #187 안 (d)).

`/pose` 인증은 **앱 번들에 든 공유 토큰 하나**뿐이고 `session_id` 는 AUTO_INCREMENT 순차
정수다. 즉 토큰을 뽑은 사람이 번호만 바꿔 가며 남의 세션에 프레임을 넣을 수 있었고, 그
데이터는 AI 를 거쳐 Spring DB 까지 갔다. 세션마다 다른 비밀값이 그 둘을 가른다.

이 파일이 고정하는 것 셋:
    1. 값이 다르면 프레임이 판정에 못 들어간다
    2. 그 거절이 **«세션 없음» 과 구분되지 않는다** — 구분되면 공격자가 session_id 를 훑어
       살아있는 세션을 열거할 수 있다. 막으려던 것의 절반을 응답으로 되돌려주는 셈이다
    3. **값을 안 보내면 막힌다**(2단계 강제). 1단계에서는 이것도 통과였고, 그 창이 곧
       방어의 부재였다
    4. 세션에 보관값이 없으면(배포 전 세션) 여전히 통과 — 스스로 닫히는 창이다

⚠️ **여기서 재는 것은 대조이지 검출 품질이 아니다.** MediaPipe 는 mock 으로 갈음한다.
"""
import unittest
from unittest import mock

import numpy as np

from app.api.endpoints import pose as pose_endpoint
from app.grpc.session_state import SessionStateRegistry, get_registry
from app.models.pose import PoseRequest, PoseSkipReason

from tests.test_squat_analyzer import _frame

_BLANK_IMAGE = np.zeros((4, 4, 3), dtype=np.uint8)
_STANDING_ANGLE = 172.0
_REF = [[90.0, 170.0], [80.0, 165.0]]

_OWNER_NONCE = "9Xk2QwErTyUiOpAsDfGhJk"
_ATTACKER_NONCE = "aaaaaaaaaaaaaaaaaaaaaa"


def _fake_lease(detect_fn):
    class _L:
        def __enter__(self):
            return mock.Mock(detect=detect_fn)

        def __exit__(self, *exc):
            return False

    return lambda _session_id: _L()


class SessionNonceOwnershipTest(unittest.TestCase):
    """대조가 실제로 프레임을 막는가 — 엔드포인트 층."""

    def _session(self, session_id, session_nonce):
        get_registry().create(
            session_id=session_id,
            exercise_id=1,
            reference_angles=_REF,
            session_nonce=session_nonce,
        )
        self.addCleanup(get_registry().remove, session_id)

    def _post(self, session_id, session_nonce):
        req = PoseRequest(
            image="", session_id=session_id, exercise_type="squat",
            session_nonce=session_nonce,
        )
        detect_fn = lambda _img: _frame(_STANDING_ANGLE)
        with mock.patch.object(pose_endpoint, "base64_to_image", lambda _: _BLANK_IMAGE), \
             mock.patch.object(pose_endpoint, "lease_detector", _fake_lease(detect_fn)):
            return pose_endpoint.detect_pose(req)

    def test_맞는_값이면_판정에_들어간다(self):
        self._session(9001, _OWNER_NONCE)

        res = self._post(9001, _OWNER_NONCE)

        self.assertTrue(res.success)
        self.assertIsNone(res.skip_reason)

    def test_다른_값이면_프레임이_버려진다(self):
        self._session(9002, _OWNER_NONCE)

        res = self._post(9002, _ATTACKER_NONCE)

        self.assertFalse(res.success)
        self.assertEqual(res.skip_reason, PoseSkipReason.SESSION_NOT_FOUND)

    def test_거절이_없는_세션과_구분되지_않는다(self):
        """🔴 이 테스트가 이 파일의 핵심이다.

        «세션은 있는데 네 것이 아니다» 를 알려주면, 공격자는 session_id 를 훑는 것만으로
        **어느 번호가 살아있는지** 알아낸다. 두 응답이 한 글자라도 다르면 그 채널이 열린다.
        """
        self._session(9003, _OWNER_NONCE)

        rejected = self._post(9003, _ATTACKER_NONCE)
        missing = self._post(9999, _ATTACKER_NONCE)   # 만든 적 없는 세션

        self.assertEqual(rejected.success, missing.success)
        self.assertEqual(rejected.skip_reason, missing.skip_reason)
        # 메시지는 session_id 만 다르다 — 그 번호는 공격자가 이미 아는 값이라 새 정보가 아니다.
        self.assertEqual(
            rejected.message.replace("9003", "N"), missing.message.replace("9999", "N")
        )

    def test_값을_안_보내면_막힌다_2단계_강제(self):
        """1단계에서는 통과였다. 그 창을 닫는 것이 2단계의 전부다.

        «안 보냄» 과 «틀림» 이 같은 응답이어야 한다 — 다르면 «이 세션은 nonce 를 요구한다»
        는 사실이 새어 나가고, 그것만으로도 살아있는 세션을 가려낼 수 있다.
        """
        self._session(9004, _OWNER_NONCE)

        missing = self._post(9004, None)
        wrong = self._post(9004, _ATTACKER_NONCE)

        self.assertFalse(missing.success)
        self.assertEqual(missing.skip_reason, PoseSkipReason.SESSION_NOT_FOUND)
        self.assertEqual(missing.skip_reason, wrong.skip_reason)
        self.assertEqual(missing.message, wrong.message)

    def test_배포_전_세션은_무엇을_보내도_통과한다(self):
        """보관값이 없는 세션은 강제를 켠 뒤에도 통과한다.

        🔴 구멍처럼 보이지만 **여기에 도달할 수 있는 세션이 사실상 없다.** 배포 후 만들어진
        세션은 항상 값을 갖고(Spring 이 무조건 발급), 배포 전 세션의 AI 상태는 registry 가
        프로세스 메모리라 **재배포로 통째로 사라진다** — 그런 요청은 이 검사에 닿기 전에
        «세션 없음» 으로 떨어진다. 실제로 이 분기가 열리는 경로는 **재부착 하나**뿐이고,
        공격자가 «보관값 없는 상태» 를 만들 수단은 없다.

        (이 테스트는 그 분기가 열렸을 때의 계약을 고정한다 — 도달 경로가 좁다는 것과
        동작이 정의돼 있어야 한다는 것은 다른 얘기다.)
        """
        # ⚠️ 세션을 둘로 나눈다. 같은 세션에 연속으로 쏘면 **유입 상한(#143, 300ms)** 에 걸려
        #    두 번째가 RATE_LIMITED 로 떨어지고, 그러면 이 테스트가 nonce 가 아니라 리미터를
        #    재게 된다(실제로 한 번 그렇게 실패했다).
        self._session(9005, None)
        self._session(9006, None)

        self.assertTrue(self._post(9005, _ATTACKER_NONCE).success)
        self.assertTrue(self._post(9006, None).success)


class SessionNonceRegistryTest(unittest.TestCase):
    """재부착이 보관값을 어떻게 다루는가 — 레지스트리 층."""

    def test_재부착이_배포_전_세션에_값을_채운다(self):
        """배포 전에 시작돼 배포 후 재부착으로 돌아온 세션이 이 경로로 신원을 얻는다."""
        reg = SessionStateRegistry()
        reg.create(session_id=1, exercise_id=1, reference_angles=_REF, session_nonce=None)

        state, already_active = reg.create_if_absent(
            session_id=1, exercise_id=1, reference_angles=_REF, session_nonce=_OWNER_NONCE
        )

        self.assertTrue(already_active)
        self.assertEqual(state.session_nonce, _OWNER_NONCE)

    def test_이미_있는_값은_덮지_않는다(self):
        """Spring 은 같은 DB 행에서 읽으므로 같은 값을 보낸다. 다르다면 둘 중 하나가 틀린
        것이지 «새 값이 맞다» 가 아니다 — 조용히 덮으면 재부착이 신원 교체 수단이 된다."""
        reg = SessionStateRegistry()
        reg.create(session_id=1, exercise_id=1, reference_angles=_REF,
                   session_nonce=_OWNER_NONCE)

        state, _ = reg.create_if_absent(
            session_id=1, exercise_id=1, reference_angles=_REF,
            session_nonce=_ATTACKER_NONCE,
        )

        self.assertEqual(state.session_nonce, _OWNER_NONCE)

    def test_새_세션은_받은_값을_보관한다(self):
        reg = SessionStateRegistry()

        state, already_active = reg.create_if_absent(
            session_id=2, exercise_id=1, reference_angles=_REF, session_nonce=_OWNER_NONCE
        )

        self.assertFalse(already_active)
        self.assertEqual(state.session_nonce, _OWNER_NONCE)


if __name__ == "__main__":
    unittest.main()
