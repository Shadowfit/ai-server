"""BACK_BENT 감지기 배선 (#193·#228) — pose.py 의 `report_feedback_batch` 호출 조건.

절대 임계값(35°, `squat_analyzer.analyze_squat_frames:228` 재사용)을 기존 3종 심각도 게이트와
결합해 "무슨 결함인지"를 정한다. 판정 자체(각도 계산)는 `_torso_tilt_degrees` 재사용이라 여기서
다시 재지 않는다 — 이 파일이 고정하는 것은 **그 결과로 호출하느냐 마느냐**뿐이다.
"""

import unittest
from unittest import mock

from app.api.endpoints import pose as pose_endpoint
from app.grpc import spring_client
from app.grpc.session_state import get_registry
from app.models.pose import PoseRequest

from tests.test_pose_response_contract import _BLANK_IMAGE, _fake_lease
from tests.test_squat_analyzer import _frame

# waiting_for_standing → ready → bottom → standing 전이가 StreamingSquatAnalyzer 의 3프레임
# rolling smoothing 을 확실히 수렴시키도록 넉넉히 반복한다 (정확한 최소 프레임 수를 세지 않고
# 여유를 둔다 — 이 테스트의 관심사는 감지기 튜닝이 아니라 콜백 배선이다).
_STANDING = 172.0
_BOTTOM = 90.0
_REP_SEQUENCE = [_STANDING] * 4 + [_BOTTOM] * 8 + [_STANDING] * 4


class BackBentFeedbackCallbackTest(unittest.TestCase):
    def _session(self, session_id: int):
        # reference_angles=[] → _summarize_rep 이 sync_rate=0.0 을 내므로, 게이트("자세 양호"
        # 아님)가 항상 트리거된다 — "무슨 유형인지"만 기울기 값에 따라 갈리게 만드는 장치다.
        state = get_registry().create(session_id=session_id, exercise_id=1, reference_angles=[])
        self.addCleanup(get_registry().remove, session_id)
        return state

    def _run_rep(self, session_id: int, torso_tilt: float):
        frames = iter(_frame(angle, torso_tilt=torso_tilt) for angle in _REP_SEQUENCE)
        req = PoseRequest(image="", session_id=session_id, exercise_type="squat")
        with mock.patch.object(pose_endpoint, "base64_to_image", lambda _: _BLANK_IMAGE), \
            mock.patch.object(
                pose_endpoint, "lease_detector", _fake_lease(lambda _img: next(frames))
            ), \
            mock.patch.object(pose_endpoint, "accept_frame", lambda _s, _n: True), \
            mock.patch.object(pose_endpoint.spring_client, "report_pose_data_batch"), \
            mock.patch.object(pose_endpoint.spring_client, "report_feedback_batch") as mock_fb:
            # flush_pending_feedback 이 outcome 을 언패킹한다 — 기본 반환을 성공으로 둬서
            # 버퍼가 정상적으로 비워지는 경로를 태운다(그래야 무한루프 없이 한 번에 끝난다).
            mock_fb.return_value = (spring_client.FeedbackBatchOutcome.OK, 1)
            responses = [pose_endpoint.detect_pose(req) for _ in _REP_SEQUENCE]
        return responses, mock_fb

    def test_기울기가_35도를_넘고_게이트도_실패하면_BACK_BENT_를_보낸다(self) -> None:
        self._session(9301)
        responses, mock_fb = self._run_rep(9301, torso_tilt=45.0)

        self.assertTrue(
            any(r.rep_completed for r in responses), "테스트 전제: rep 이 완성돼야 한다"
        )
        mock_fb.assert_called_once()
        session_id, set_no, is_final, events = mock_fb.call_args.args
        self.assertEqual(session_id, 9301)
        self.assertEqual(set_no, 1)
        self.assertFalse(is_final)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].feedback_type, "BACK_BENT")

    def test_기울기가_35도_밑이면_유형을_안_보낸다(self) -> None:
        self._session(9302)
        responses, mock_fb = self._run_rep(9302, torso_tilt=5.0)

        self.assertTrue(any(r.rep_completed for r in responses))
        mock_fb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
