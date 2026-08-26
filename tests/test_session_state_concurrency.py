"""accept_frame 동시성 회귀 테스트 (#162).

SessionStateRegistry._lock 은 dict 접근(get/create/remove)만 보호하고, get() 이 돌려준
SessionState 필드 변경은 무방비였다 — 같은 세션의 요청 둘이 동시에 들어오면 accept_frame 의
데드라인 read-modify-write 가 경합해 유입 속도 상한이 우회될 수 있었다.

자연 발생 재현은 어렵다(GIL 스위치 창이 좁다) — 이 테스트는 스레드 간 강제 인터리브로
race window 를 항상 여는 방식으로, 순수 스케줄링 운에 기대지 않고 결정적으로 회귀를 잡는다.
"""

import threading
import time
import unittest

from app.grpc.session_state import SessionState, accept_frame


class AcceptFrameRaceTest(unittest.TestCase):
    def test_unlocked_call_can_double_accept(self):
        """락 없이 부르면(#162 이전 동작) 경합이 실제로 발생함을 증명 — 회귀 기준선."""
        state = SessionState(session_id=1, exercise_id=1)
        now = time.monotonic()
        gate = threading.Event()
        proceed = threading.Event()

        def racer_a():
            deadline = state.next_frame_deadline  # accept_frame 247행과 동일한 읽기
            gate.set()
            proceed.wait()  # b 가 자기 읽기를 끝낼 때까지 강제로 붙잡아둔다
            if deadline is None:
                state.next_frame_deadline = now + 0.3
                state.accepted_frame_count += 1

        def racer_b():
            gate.wait()
            deadline = state.next_frame_deadline  # a 가 아직 안 썼으므로 여전히 None
            proceed.set()
            if deadline is None:
                state.next_frame_deadline = now + 0.3
                state.accepted_frame_count += 1

        t1 = threading.Thread(target=racer_a)
        t2 = threading.Thread(target=racer_b)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(
            state.accepted_frame_count, 2,
            "이 단언이 깨지면 좋은 소식이다 — 다만 그건 accept_frame 내부 구현이 바뀌어 이 "
            "테스트의 인터리브 지점이 더 이상 실제 코드 경로와 안 맞는다는 뜻이니 재점검할 것",
        )

    def test_locked_call_accepts_exactly_once(self):
        """호출부가 state.state_lock 을 쥔 채로 부르면(#162 수정 후, pose.py 의 실제 패턴)
        동시 요청 중 정확히 하나만 수락된다."""
        state = SessionState(session_id=1, exercise_id=1)
        now = time.monotonic()

        results = []
        results_lock = threading.Lock()

        def call_locked():
            with state.state_lock:
                ok = accept_frame(state, now)
            with results_lock:
                results.append(ok)

        threads = [threading.Thread(target=call_locked) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(state.accepted_frame_count, 1)
        self.assertEqual(sum(1 for ok in results if ok), 1)
        self.assertEqual(sum(1 for ok in results if not ok), 7)

    def test_state_lock_is_per_session(self):
        """세션마다 독립된 락이어야 한다 — 아니면 무관한 세션끼리 서로 막는다."""
        a = SessionState(session_id=1, exercise_id=1)
        b = SessionState(session_id=2, exercise_id=1)
        self.assertIsNot(a.state_lock, b.state_lock)


if __name__ == "__main__":
    unittest.main()
