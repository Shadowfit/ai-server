"""DetectorPool.acquire() 동시성 회귀 테스트 (#599).

PoseDetector() 생성(MediaPipe 그래프 초기화)이 예전엔 DetectorPool._guard 안에서 돌아,
세션 하나를 만드는 동안 풀 전체(다른 세션의 acquire·release·lease)가 막혔다
(실측: loadtest/results/grpc-threadpool-sizing-reattach-593/README.md). 이 테스트는 그
직렬화가 없어졌는지, 그리고 그 대가로 새로 생긴 동시성(같은 session_id 중복 호출·용량
계산·생성 실패 시 정리)이 안전한지를 강제 인터리브로 결정적으로 검증한다.

`test_session_state_concurrency.py`와 같은 방식 — 타이밍에 기대지 않고 threading.Event로
경합 지점을 강제로 연다.

⚠️ `unittest.mock.patch`는 대상 모듈 속성을 **스레드 무관하게 전역으로** 바꾼다. 그래서
patch 컨텍스트를 스레드 함수 안에서 열고 닫으면, 다른 스레드가 아직 그 안에서 도는 동안
컨텍스트가 먼저 닫히거나(복원 경합) 또는 이 테스트처럼 아직 안 닫혔는데 다른 스레드가
그 patch를 "같이" 보는 상황이 생긴다. 그래서 여기서는 **patch를 테스트 본문 하나에서만
열고, 그 안에서 스레드를 띄운다** — 스레드 함수 자체는 patch를 모른다.
"""

import threading
import time
import unittest
from unittest.mock import patch

from app.core.mediapipe_detector import DetectorPool


class _FailingDetector:
    def __init__(self):
        raise RuntimeError("모델 로드 실패(시뮬레이션)")


class DetectorPoolConcurrencyTest(unittest.TestCase):
    def test_other_session_not_blocked_by_slow_construction(self):
        """세션 A가 생성 중이어도 무관한 세션 B의 acquire는 안 막혀야 한다 — #599 핵심 회귀."""
        pool = DetectorPool(capacity=10)
        gate = threading.Event()          # 첫 생성(A)을 사람이 통제해서 "느린 생성"을 흉내낸다
        started = threading.Event()
        call_count = [0]
        call_lock = threading.Lock()

        def make(*_a, **_kw):
            with call_lock:
                call_count[0] += 1
                is_first = call_count[0] == 1
            if is_first:
                started.set()
                gate.wait(timeout=5)      # A는 gate가 열릴 때까지 붙잡힌다
            return object()

        with patch("app.core.mediapipe_detector.PoseDetector", side_effect=make):
            results = {}

            def acquire_a():
                results["a"] = pool.acquire(session_id=1)

            t_a = threading.Thread(target=acquire_a)
            t_a.start()
            self.assertTrue(started.wait(timeout=5), "세션 A의 생성이 시작되지 않았다")

            # A가 아직 gate에 막혀 있는 상태에서, 무관한 세션 B를 메인 스레드에서 재본다.
            t0 = time.monotonic()
            ok_b = pool.acquire(session_id=2)
            elapsed_b = time.monotonic() - t0

            gate.set()
            t_a.join(timeout=5)

        self.assertTrue(results["a"])
        self.assertTrue(ok_b)
        self.assertLess(
            elapsed_b, 1.0,
            "B가 A의 생성 완료(gate)를 기다렸다면 여기서 5초 가까이 걸린다 — "
            "즉 락이 여전히 생성 전체를 감싸고 있다는 뜻(#599 재발)",
        )

    def test_duplicate_call_same_session_builds_once(self):
        """같은 session_id로 동시에 여러 번 불려도 PoseDetector()는 한 번만 만든다."""
        pool = DetectorPool(capacity=10)
        counter = [0]
        counter_lock = threading.Lock()
        release_gate = threading.Event()

        def make(*_a, **_kw):
            with counter_lock:
                counter[0] += 1
            release_gate.wait(timeout=5)
            return object()

        results = []
        results_lock = threading.Lock()

        def caller():
            ok = pool.acquire(session_id=42)
            with results_lock:
                results.append(ok)

        with patch("app.core.mediapipe_detector.PoseDetector", side_effect=make):
            threads = [threading.Thread(target=caller) for _ in range(5)]
            for t in threads:
                t.start()
            time.sleep(0.2)          # builder 하나가 먼저 _building 예약을 잡을 시간을 준다
            release_gate.set()       # builder의 생성을 완료시킨다 — 대기자들도 같이 풀린다
            for t in threads:
                t.join(timeout=5)

        self.assertEqual(counter[0], 1, "PoseDetector()가 두 번 이상 생성됐다 — 중복 생성 방지 실패")
        self.assertEqual(results, [True] * 5)

    def test_capacity_counts_in_flight_builds(self):
        """생성 중인 세션도 용량에 포함돼야 한다 — 안 그러면 동시 요청이 capacity를 넘는다."""
        pool = DetectorPool(capacity=1)
        gate = threading.Event()
        started = threading.Event()

        def make(*_a, **_kw):
            started.set()
            gate.wait(timeout=5)
            return object()

        with patch("app.core.mediapipe_detector.PoseDetector", side_effect=make):
            t = threading.Thread(target=lambda: pool.acquire(session_id=1))
            t.start()
            self.assertTrue(started.wait(timeout=5))

            # 첫 세션이 아직 생성 중(용량 1개를 이미 예약)인 상태에서 다른 세션을 시도
            # — capacity=1이라 거절돼야 한다. self._detectors는 아직 비어 있지만
            # self._building에 1개가 있으므로 그걸 세지 않으면 이 assert가 깨진다.
            ok_second = pool.acquire(session_id=2)

            gate.set()
            t.join(timeout=5)

        self.assertFalse(ok_second, "생성 중인 세션이 용량 계산에서 빠져 capacity를 넘겨 받았다")

        used, cap = pool.status()
        self.assertEqual((used, cap), (1, 1))

    def test_construction_failure_propagates_and_cleans_up(self):
        """생성 실패는 삼키지 않고 그대로 올린다. 그리고 재시도가 가능해야 한다(정리 확인)."""
        pool = DetectorPool(capacity=10)

        with patch("app.core.mediapipe_detector.PoseDetector", side_effect=_FailingDetector):
            with self.assertRaises(RuntimeError):
                pool.acquire(session_id=7)

        used, _ = pool.status()
        self.assertEqual(used, 0, "실패한 예약이 _building에 남아 용량을 계속 잡아먹는다")

        # 실패 후 재시도는 정상적으로(진짜 PoseDetector로) 성공해야 한다.
        try:
            ok = pool.acquire(session_id=7)
            self.assertTrue(ok)
        finally:
            pool.release(session_id=7)


if __name__ == "__main__":
    unittest.main()
