"""AI → Spring 완료 콜백용 **상한 있는** 데몬 스레드 풀 (#614).

`StopAnalysis` 는 호출될 때마다 `threading.Thread` 를 새로 만들어 `CompleteAnalysis` 콜백을
보냈다 — 풀도 상한도 없었다. 콜백은 실패 시 최대 3회 × 5초 데드라인이라 한 스레드가 15초 넘게
살 수 있고, Spring 이 느리거나 죽은 동안 완료가 몰리면 스레드가 수백 개로 불어난다(EC2 실측
90초에 450개 초과, loadtest/results/grpc-reattach-thread-cpu-ec2-2026-08-30). 그 스레드들이
`logging` 전역 락을 두고 경합해 gRPC 워커 전체가 묶였다 — Spring 장애가 AI 서버로 번지는 경로다.

여기서는 스레드 수를 고정하고 **넘치는 작업은 큐에 세운다**. 큐는 상한이 없다 — 상한을 두면
버릴 것을 골라야 하는데, 완료 콜백은 유실되면 세션이 IN_PROGRESS 로 남는 결과라 버릴 후보가
없다(Spring 쪽 타임아웃 스케줄러가 뒷정리하지만 그건 «유실 뒤 복구» 지 «전달» 이 아니다).
대신 큐 깊이를 게이지로 내보내 «쌓이고 있다» 가 보이게 한다.

`concurrent.futures.ThreadPoolExecutor` 를 안 쓴 이유: 그 워커는 non-daemon 이라 인터프리터
종료 시 atexit 이 남은 작업을 전부 기다린다. Spring 이 죽어 있는 채로 재배포하면 종료가 콜백
재시도(최대 ~17초/건)에 붙잡힌다 — 예전 `daemon=True` 스레드엔 없던 지연이다. 이 풀의 스레드는
데몬이라 종료를 안 막고, 큐에 남은 콜백은 예전과 같이 «허용되는 손실» 이다.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable

logger = logging.getLogger(__name__)


class CallbackPool:
    """고정 개수의 데몬 스레드가 하나의 FIFO 큐를 소비한다. 스레드는 첫 submit 때 만든다."""

    def __init__(self, workers: int, name: str = "complete-callback") -> None:
        if workers < 1:
            raise ValueError(f"workers 는 1 이상이어야 한다: {workers}")
        self._workers = workers
        self._name = name
        self._queue: queue.Queue[tuple[Callable[..., None], tuple]] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._start_lock = threading.Lock()
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()

    @property
    def workers(self) -> int:
        return self._workers

    def submit(self, fn: Callable[..., None], *args) -> None:
        """실행을 예약한다. 절대 블로킹하지 않는다 — 호출자는 gRPC 워커 스레드다."""
        self._ensure_started()
        self._queue.put((fn, args))

    def pending(self) -> int:
        """큐에서 기다리는 작업 수 (실행 중인 것은 제외)."""
        return self._queue.qsize()

    def in_flight(self) -> int:
        """지금 실행 중인 작업 수 (≤ workers)."""
        with self._in_flight_lock:
            return self._in_flight

    def thread_count(self) -> int:
        return sum(1 for t in self._threads if t.is_alive())

    def _ensure_started(self) -> None:
        if self._threads:
            return
        with self._start_lock:
            if self._threads:
                return
            for i in range(self._workers):
                t = threading.Thread(target=self._run, name=f"{self._name}-{i}", daemon=True)
                t.start()
                self._threads.append(t)

    def _run(self) -> None:
        while True:
            fn, args = self._queue.get()
            with self._in_flight_lock:
                self._in_flight += 1
            try:
                fn(*args)
            except Exception:  # noqa: BLE001 — 콜백 하나가 죽어도 워커 스레드는 살아야 한다
                logger.exception("[%s] 콜백 실행 중 예외 — 워커는 계속 돈다", self._name)
            finally:
                with self._in_flight_lock:
                    self._in_flight -= 1
                self._queue.task_done()
