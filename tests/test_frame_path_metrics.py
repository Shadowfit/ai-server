"""프레임 경로 계측 단위 테스트 (decisions/ai-receive-path-scaling.md §12).

⚠️ **이 테스트가 실제로 지키는 것은 하나다** — 「미들웨어가 심은 시각표가 **다른 스레드에서
도는 동기 핸들러**에서도 보이는가」. FastAPI 는 `def` 핸들러를 threadpool 로 보내므로
contextvar 가 안 넘어가면 `wait`·`infer` 가 통째로 조용히 비고, 계측은 **0 을 돌려주는 대신
아무 말도 안 하게** 된다. 그 침묵이 이 계측의 유일한 실패 모드다.

MediaPipe 를 안 띄우려고 `app.main` 대신 같은 모양의 최소 앱을 세운다(test_auth_middleware 와 같은 방식).
"""

from __future__ import annotations

import threading
import time

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.observability import frame_path


def _make_client(capacity: int = 64) -> tuple[TestClient, frame_path.Recorder]:
    frame_path._recorder = None
    rec = frame_path.install(capacity)

    router = APIRouter(prefix="/api/v1")

    @router.post("/pose")
    def pose(body: dict) -> dict:                # 동기 핸들러 — threadpool 로 간다
        # 🔴 `try/finally` 로 감싸는 것은 **실제 핸들러와 같은 모양이어야** 하기 때문이다
        #    (`app/api/endpoints/pose.py`). 이걸 안 찍으면 `post_app`·`post_loop` 가 통째로
        #    비는데, 그게 정확히 #509 였다 — 구간이 늘었는데 픽스처만 옛 표지에 머물렀다.
        try:
            frame_path.mark_handler_in()
            time.sleep(0.002)
            frame_path.mark_decoded()
            frame_path.mark_leased()
            time.sleep(0.002)
            frame_path.mark_inferred()
            return {"ok": True}
        finally:
            frame_path.mark_handler_out()

    @router.post("/pose-early")
    def pose_early(body: dict) -> dict:          # 조기 반환 — 뒷 구간이 안 찍힌다
        frame_path.mark_handler_in()
        return {"ok": False}

    app = FastAPI()
    app.include_router(router)
    app.include_router(frame_path.router, prefix="/api/v1")
    app.add_middleware(
        frame_path.FramePathMiddleware, recorder=rec, path="/api/v1/pose"
    )
    return TestClient(app), rec


def test_spans_are_recorded_across_the_threadpool_boundary():
    """🔴 핵심 — 미들웨어(이벤트 루프)와 핸들러(워커 스레드)가 같은 시각표를 본다."""
    client, rec = _make_client()
    client.post("/api/v1/pose", json={})

    snap = rec.snapshot()
    assert snap["requests"] == 1
    assert snap["partial"] == 0, "모든 구간이 찍혔어야 한다"

    for span in frame_path.SPANS:
        assert snap["spans"][span]["n"] == 1, f"{span} 구간이 비었다"

    # 핸들러가 4ms 를 자게 했으므로 total 은 그보다 크고, wait 는 total 안에 있다.
    assert snap["spans"]["total"]["max_ms"] >= 4.0
    assert snap["spans"]["wait"]["max_ms"] <= snap["spans"]["total"]["max_ms"]


def test_early_return_leaves_later_spans_empty_not_zero():
    """조기 반환이면 뒷 구간은 **비어야** 한다 — 0ms 로 채우면 «빠르다» 로 읽힌다."""
    client, rec = _make_client()
    client.post("/api/v1/pose-early", json={})   # 계측 경로가 아니다 → 아예 안 잡힌다
    assert rec.snapshot()["requests"] == 0

    # 계측 경로로 같은 모양을 만든다: mark_handler_in 만 찍고 끝나는 경우
    tr = frame_path.FrameTrace(t_asgi_in=1.0)
    tr.t_handler_in = 1.001
    tr.t_asgi_out = 1.002
    spans = frame_path._spans_of(tr)
    assert "wait" in spans and "total" in spans
    assert "infer" not in spans and "lease" not in spans

    described = frame_path._describe([], seen=0)
    assert described == {"n": 0, "seen": 0}, "표본 없음을 0ms 로 적으면 안 된다"


def test_middleware_ignores_other_paths():
    client, rec = _make_client()
    client.get("/api/v1/diag/frame-path")
    assert rec.snapshot()["requests"] == 0


def test_inflight_returns_to_zero_and_concurrency_is_counted():
    client, rec = _make_client()

    def hit() -> None:
        client.post("/api/v1/pose", json={})

    threads = [threading.Thread(target=hit) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = rec.snapshot()
    assert snap["requests"] == 4
    # 🔴 짝이 맞는가 — 안 맞으면 인플라이트가 새고 「동시 처리 수」가 통째로 거짓말이 된다.
    assert snap["inflight_http"] == 0
    assert snap["inflight_handler"] == 0
    assert sum(snap["handler_concurrency"].values()) == 4
    assert snap["inflight_handler_max"] >= 1


def test_reset_clears_samples_but_not_live_inflight():
    client, rec = _make_client()
    client.post("/api/v1/pose", json={})
    assert rec.snapshot()["requests"] == 1

    rec.reset()
    snap = rec.snapshot()
    assert snap["requests"] == 0
    assert snap["spans"]["total"]["n"] == 0
    assert snap["inflight_http"] == 0


def test_marks_are_noop_when_disabled():
    """계측 OFF = contextvar 가 비어 있다. 핸들러가 mark 를 불러도 조용히 지나가야 한다."""
    frame_path._recorder = None
    frame_path._current.set(None)
    frame_path.mark_handler_in()
    frame_path.mark_decoded()
    frame_path.mark_leased()
    frame_path.mark_inferred()
    assert frame_path.get_recorder() is None


def test_ring_keeps_recent_samples_and_reports_total_seen():
    """링이 가득 차면 최근 것만 남는다 — 그 사실을 `seen` 이 드러내야 한다."""
    rec = frame_path.Recorder(capacity=3)
    for i in range(5):
        base = 100.0                       # perf_counter() 원점은 임의다 — 0 을 쓰지 않는다
        tr = frame_path.FrameTrace(t_asgi_in=base)
        tr.t_handler_in = tr.t_decoded = tr.t_leased = base
        tr.t_inferred = tr.t_response = tr.t_asgi_out = base + float(i) / 1000.0
        rec.enter_http()
        rec.commit(tr)

    total = rec.snapshot()["spans"]["total"]
    assert total["n"] == 3, "링 용량만큼만 남는다"
    assert total["seen"] == 5, "덮인 것까지 세야 «최근 N 개의 p99» 임을 알 수 있다"
