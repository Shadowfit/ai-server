"""CompleteAnalysis 콜백 풀 — 스레드 상한·순서·예외 격리 (#614)."""

from __future__ import annotations

import threading
import time

import pytest

from app.grpc.callback_pool import CallbackPool
from app.grpc.correlation import _correlation_id, get_correlation_id
from app.grpc.correlation import wrap as correlation_wrap


def test_thread_count_is_bounded_under_burst():
    """StopAnalysis 가 초당 수십 번 와도 스레드는 workers 개를 안 넘는다 — 이슈의 450개 그림이 안 나온다."""
    pool = CallbackPool(workers=3)
    release = threading.Event()
    started = threading.Semaphore(0)
    done = []

    def slow(i):
        started.release()
        release.wait(5)
        done.append(i)

    for i in range(50):
        pool.submit(slow, i)

    # 3개가 실행에 들어갈 때까지 기다린다 — 나머지 47개는 큐에 있어야 한다
    for _ in range(3):
        assert started.acquire(timeout=5)
    time.sleep(0.05)
    assert pool.thread_count() == 3
    assert pool.in_flight() == 3
    assert pool.pending() == 47
    assert threading.active_count() >= 3

    release.set()
    deadline = time.time() + 5
    while len(done) < 50 and time.time() < deadline:
        time.sleep(0.01)
    assert sorted(done) == list(range(50))
    assert pool.pending() == 0
    assert pool.thread_count() == 3


def test_submit_never_blocks_caller():
    """호출자는 gRPC 워커 스레드다 — 풀이 꽉 차 있어도 submit 은 즉시 돌아와야 한다."""
    pool = CallbackPool(workers=1)
    release = threading.Event()
    pool.submit(lambda: release.wait(5))
    t0 = time.perf_counter()
    for _ in range(100):
        pool.submit(lambda: None)
    assert time.perf_counter() - t0 < 0.5
    release.set()


def test_exception_in_one_callback_does_not_kill_worker():
    pool = CallbackPool(workers=1)
    seen = []

    def boom():
        raise RuntimeError("콜백 실패")

    pool.submit(boom)
    pool.submit(seen.append, "after")
    deadline = time.time() + 5
    while not seen and time.time() < deadline:
        time.sleep(0.01)
    assert seen == ["after"]
    assert pool.thread_count() == 1


def test_correlation_survives_when_wrapped():
    """풀 스레드는 ContextVar 를 상속하지 않는다 — 호출자가 correlation_wrap 으로 감싸면 이어진다."""
    pool = CallbackPool(workers=1)
    seen = []
    token = _correlation_id.set("cid-614")
    try:
        pool.submit(correlation_wrap(lambda: seen.append(get_correlation_id())))
    finally:
        _correlation_id.reset(token)
    deadline = time.time() + 5
    while not seen and time.time() < deadline:
        time.sleep(0.01)
    assert seen == ["cid-614"]


def test_workers_must_be_positive():
    with pytest.raises(ValueError):
        CallbackPool(workers=0)
