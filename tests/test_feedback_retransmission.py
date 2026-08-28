"""`flush_pending_feedback`의 버퍼 상태 전이 (#193 재전송,
docs/decisions/feedback-batch-retransmission.md §7).

pose.py 의 rep 완성 배선을 다시 타지 않고, `SessionState.pending_feedback_events` 와
`spring_client.report_feedback_batch`(mock)만으로 상태기계 자체를 고정한다 —
"어떤 outcome 이 버퍼를 어떻게 바꾸는가"가 이 파일의 관심사다.
"""

import unittest
from unittest import mock

from app.api.endpoints.pose import flush_pending_feedback
from app.grpc import spring_client
from app.grpc.session_state import SessionState


def _event(rep_number: int) -> spring_client.PendingFeedbackEvent:
    return spring_client.PendingFeedbackEvent(
        feedback_type="BACK_BENT", rep_number=rep_number, sync_rate_at_trigger=10.0
    )


class FlushPendingFeedbackTest(unittest.TestCase):
    def _state(self, *events: spring_client.PendingFeedbackEvent) -> SessionState:
        state = SessionState(session_id=1, exercise_id=1)
        state.pending_feedback_events = list(events)
        return state

    def test_OK_이면_버퍼를_끝까지_비운다(self) -> None:
        state = self._state(_event(1), _event(2), _event(3))
        with mock.patch.object(spring_client, "report_feedback_batch") as mock_fb:
            mock_fb.return_value = (spring_client.FeedbackBatchOutcome.OK, 1)
            flush_pending_feedback(state)

        self.assertEqual(state.pending_feedback_events, [])
        self.assertEqual(mock_fb.call_count, 3)

    def test_TRANSIENT_이면_버퍼를_유지하고_거기서_멈춘다(self) -> None:
        state = self._state(_event(1), _event(2))
        with mock.patch.object(spring_client, "report_feedback_batch") as mock_fb:
            mock_fb.return_value = (spring_client.FeedbackBatchOutcome.TRANSIENT, 0)
            flush_pending_feedback(state)

        self.assertEqual(len(state.pending_feedback_events), 2, "실패분을 버리면 안 된다")
        mock_fb.assert_called_once()

    def test_SESSION_GONE_이면_버퍼_전체를_버린다(self) -> None:
        state = self._state(_event(1), _event(2))
        with mock.patch.object(spring_client, "report_feedback_batch") as mock_fb:
            mock_fb.return_value = (spring_client.FeedbackBatchOutcome.SESSION_GONE, 0)
            flush_pending_feedback(state)

        self.assertEqual(state.pending_feedback_events, [], "세션이 사라졌으니 재시도해도 소용없다")
        mock_fb.assert_called_once()

    def test_INVALID_이면_그_건만_버리고_다음_건으로_넘어간다(self) -> None:
        state = self._state(_event(1), _event(2))
        with mock.patch.object(spring_client, "report_feedback_batch") as mock_fb:
            mock_fb.side_effect = [
                (spring_client.FeedbackBatchOutcome.INVALID, 0),
                (spring_client.FeedbackBatchOutcome.OK, 1),
            ]
            flush_pending_feedback(state)

        self.assertEqual(state.pending_feedback_events, [])
        self.assertEqual(mock_fb.call_count, 2)
        # 두 번째 호출은 두 번째 이벤트(rep_number=2)여야 한다 — 첫 건은 버리고 다음 건으로 넘어갔다.
        second_call_events = mock_fb.call_args_list[1].args[3]
        self.assertEqual(second_call_events[0].rep_number, 2)

    def test_빈_버퍼는_아무것도_안_보낸다(self) -> None:
        state = self._state()
        with mock.patch.object(spring_client, "report_feedback_batch") as mock_fb:
            flush_pending_feedback(state)

        mock_fb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
