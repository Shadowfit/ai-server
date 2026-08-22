"""프레임 경로 계측 — 「16코어 중 9.5 만 쓰는」 이유를 앱 안에서 직접 잰다.

py-spy 가 한계에 부딪혀서 생긴 모듈이다
(`docs/decisions/ai-receive-path-scaling.md` §11 — 세 번 써서 두 번 무효, 630 샘플이 전부
파킹으로 나왔다). §12 가 「박스가 필요 없다. 필요한 건 코드다」로 설계한 계측이 이것이고,
답하려는 것은 둘이다.

① **동시 처리 수** — 「~9」는 **산술 추론**이다(`348 × 3.17ms`). 진입/이탈을 세면 직접 값이 나온다.
② **구간별 시간** — 수신 → 워커 진입 → 디코드 → **검출기 획득** → 추론 → 후처리 → 응답.
   `wait` 가 크면 「워커가 논다」가 아니라 **「워커에 못 실린다」**이고, `lease` 가 크면 후보
   2순위(검출기 획득 경로, §10-2)가 산다.

GIL 자체를 흔드는 노브(`sys.setswitchinterval`)는 여기 없다 — 기동 시 한 번 거는 것이라
`main.py` 에 있다. 이 모듈은 **재기만 한다.**

🔴 **기본은 꺼져 있다.** 켜면 이 계측이 **재려는 대상과 같은 자원(GIL·락)을 쓴다.** 요청당
   락 획득 4회 + 타임스탬프 6회다. 그래서 「계측 ON/OFF」자체가 한 판의 팔이 될 수 있고,
   절대값을 인용하려면 그 대조가 선행이다([[feedback_measure_design_needs_repeats]]).

⚠️ **판 사이에 `reset` 을 부르지 않으면 앞 판이 섞인다.** 그리고 이 계측은 프로세스 메모리라
   **재시작하면 사라진다** — 재시작이 결과를 −24% 흔든다는 관측이 있으므로(§9-4) 판마다
   재시작하는 절차와는 같이 쓰지 말 것.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from dataclasses import dataclass

from fastapi import APIRouter
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

# 재는 구간. 이름 = 스냅샷 JSON 의 키다.
#
#   wait     ASGI 진입 → 핸들러 진입   이벤트 루프 + 바디 수신 + threadpool 대기
#   decode   핸들러 진입 → 디코드 완료  base64 + cv2.cvtColor
#   lease    디코드 완료 → 리스 획득    검출기 획득 경로 (후보 2순위, §10-2)
#   infer    리스 획득 → 추론 완료      MediaPipe process()
#   post     추론 완료 → 응답 시작      분석·직렬화·Spring gRPC 콜백
#   respond  응답 시작 → ASGI 이탈      응답 전송
#   total    ASGI 진입 → ASGI 이탈
SPANS = ("wait", "decode", "lease", "infer", "post", "respond", "total")


@dataclass
class FrameTrace:
    """요청 하나의 시각표. 락 없이 이 객체에만 쓴다 — 커밋은 이탈 시점에 한 번이다."""

    t_asgi_in: float
    # 🔴 «안 찍힘» 은 None 이다. 0.0 을 sentinel 로 쓰면 «0ms 걸린 구간» 과 구분이 안 되고,
    #    perf_counter() 의 원점이 임의라 언젠가 진짜 0 근처가 나오면 조용히 틀린다.
    t_handler_in: float | None = None
    t_decoded: float | None = None
    t_leased: float | None = None
    t_inferred: float | None = None
    t_response: float | None = None
    t_asgi_out: float | None = None


# 핸들러는 threadpool 에서 도는 **다른 스레드**다. anyio 가 컨텍스트를 복사해 넘기므로
# 미들웨어가 심은 값이 핸들러에서 보이고, 객체를 **변경**하면 미들웨어 쪽에서도 보인다.
# (핸들러에서 set 한 값은 안 돌아온다 — 그래서 mutable dataclass 다.)
_current: contextvars.ContextVar[FrameTrace | None] = contextvars.ContextVar(
    "frame_trace", default=None
)


class Recorder:
    """구간 표본과 동시 처리 수를 모은다. 표본은 최근 N 개만 남는 링이다."""

    def __init__(self, capacity: int) -> None:
        self._lock = threading.Lock()
        self._cap = max(1, capacity)
        self._ring: dict[str, list[float]] = {s: [] for s in SPANS}
        self._pos: dict[str, int] = {s: 0 for s in SPANS}
        self._seen: dict[str, int] = {s: 0 for s in SPANS}  # 링에 덮인 것 포함 누적

        self.requests = 0
        self.partial = 0  # 조기 반환 등으로 구간이 다 안 찍힌 요청
        self.inflight_http = 0
        self.inflight_http_max = 0
        self.inflight_handler = 0
        self.inflight_handler_max = 0
        # 핸들러 진입 시점의 동시 처리 수 분포. **이게 「~9」를 대체하는 직접 값이다** —
        # 최대값 하나로는 «가끔 9» 와 «내내 9» 가 구분되지 않는다.
        self.handler_concurrency: dict[int, int] = {}

    # --- 핫 경로 ---------------------------------------------------------

    def enter_http(self) -> None:
        with self._lock:
            self.inflight_http += 1
            if self.inflight_http > self.inflight_http_max:
                self.inflight_http_max = self.inflight_http

    def enter_handler(self) -> None:
        with self._lock:
            self.inflight_handler += 1
            n = self.inflight_handler
            if n > self.inflight_handler_max:
                self.inflight_handler_max = n
            self.handler_concurrency[n] = self.handler_concurrency.get(n, 0) + 1

    def leave_handler(self) -> None:
        with self._lock:
            if self.inflight_handler > 0:
                self.inflight_handler -= 1

    def commit(self, tr: FrameTrace) -> None:
        """구간을 계산해 링에 넣고 http 인플라이트를 되돌린다. 요청당 락 1회."""
        spans = _spans_of(tr)
        with self._lock:
            self.inflight_http -= 1
            self.requests += 1
            if len(spans) < len(SPANS):
                self.partial += 1
            for name, ms in spans.items():
                ring = self._ring[name]
                self._seen[name] += 1
                if len(ring) < self._cap:
                    ring.append(ms)
                else:
                    ring[self._pos[name]] = ms
                    self._pos[name] = (self._pos[name] + 1) % self._cap

    # --- 읽기 ------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            rings = {s: list(v) for s, v in self._ring.items()}
            seen = dict(self._seen)
            out = {
                "requests": self.requests,
                "partial": self.partial,
                "inflight_http": self.inflight_http,
                "inflight_http_max": self.inflight_http_max,
                "inflight_handler": self.inflight_handler,
                "inflight_handler_max": self.inflight_handler_max,
                "handler_concurrency": dict(sorted(self.handler_concurrency.items())),
                "sample_capacity": self._cap,
            }
        out["spans"] = {s: _describe(rings[s], seen[s]) for s in SPANS}
        return out

    def reset(self) -> None:
        """판 사이에 부른다. 인플라이트는 **안 지운다** — 지금 도는 요청이 있다."""
        with self._lock:
            self._ring = {s: [] for s in SPANS}
            self._pos = {s: 0 for s in SPANS}
            self._seen = {s: 0 for s in SPANS}
            self.requests = 0
            self.partial = 0
            self.inflight_http_max = self.inflight_http
            self.inflight_handler_max = self.inflight_handler
            self.handler_concurrency = {}


def _spans_of(tr: FrameTrace) -> dict[str, float]:
    """찍힌 시각만으로 구간을 만든다(ms). 조기 반환이면 뒷 구간이 통째로 빠진다."""
    pairs = (
        ("wait", tr.t_asgi_in, tr.t_handler_in),
        ("decode", tr.t_handler_in, tr.t_decoded),
        ("lease", tr.t_decoded, tr.t_leased),
        ("infer", tr.t_leased, tr.t_inferred),
        ("post", tr.t_inferred, tr.t_response),
        ("respond", tr.t_response, tr.t_asgi_out),
        ("total", tr.t_asgi_in, tr.t_asgi_out),
    )
    return {
        name: (end - start) * 1000.0
        for name, start, end in pairs
        if start is not None and end is not None and end >= start
    }


def _describe(values: list[float], seen: int) -> dict:
    """표본 요약. 🔴 «표본 없음» 을 0 으로 적지 않는다 — 0ms 와 구분이 안 된다."""
    if not values:
        return {"n": 0, "seen": seen}
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "seen": seen,
        "mean_ms": round(sum(ordered) / len(ordered), 3),
        "p50_ms": round(_pct(ordered, 0.50), 3),
        "p95_ms": round(_pct(ordered, 0.95), 3),
        "p99_ms": round(_pct(ordered, 0.99), 3),
        "max_ms": round(ordered[-1], 3),
    }


def _pct(ordered: list[float], q: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


# 전역 하나. 미들웨어가 안 붙으면 아무도 안 건드린다.
_recorder: Recorder | None = None


def install(capacity: int) -> Recorder:
    global _recorder
    _recorder = Recorder(capacity)
    return _recorder


def get_recorder() -> Recorder | None:
    return _recorder


# --- 핸들러가 부르는 표시들 -------------------------------------------------
#
# 계측이 꺼져 있으면 `_current.get()` 이 None 이라 전부 즉시 반환한다. 켜져 있을 때만 값이 든다.


def mark_handler_in() -> None:
    """동기 핸들러의 첫 줄. 여기서부터가 **워커에 실린 뒤**다."""
    tr = _current.get()
    if tr is None:
        return
    tr.t_handler_in = time.perf_counter()
    rec = _recorder
    if rec is not None:
        rec.enter_handler()


def mark_decoded() -> None:
    tr = _current.get()
    if tr is not None:
        tr.t_decoded = time.perf_counter()


def mark_leased() -> None:
    tr = _current.get()
    if tr is not None:
        tr.t_leased = time.perf_counter()


def mark_inferred() -> None:
    tr = _current.get()
    if tr is not None:
        tr.t_inferred = time.perf_counter()


class FramePathMiddleware:
    """프레임 경로 전용 순수 ASGI 미들웨어.

    `BaseHTTPMiddleware` 를 안 쓴다 — 그건 요청마다 태스크와 메모리 스트림을 하나씩 더 만든다.
    지금 재려는 것이 **이벤트 루프의 여유**라, 재는 도구가 그 자원을 더 먹으면 안 된다.
    """

    def __init__(self, app: ASGIApp, recorder: Recorder, path: str) -> None:
        self.app = app
        self._rec = recorder
        self._path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != self._path:
            await self.app(scope, receive, send)
            return

        rec = self._rec
        tr = FrameTrace(t_asgi_in=time.perf_counter())
        token = _current.set(tr)
        rec.enter_http()
        handler_left = False

        async def send_wrapper(message: Message) -> None:
            # 응답이 시작되는 시점 ≈ 핸들러가 값을 돌려준 시점이다. 핸들러 안에 try/finally 를
            # 넣지 않으려고 여기서 잡는다 — 예외로 끝나도 응답은 시작되므로 짝이 맞는다.
            nonlocal handler_left
            if message["type"] == "http.response.start" and not handler_left:
                handler_left = True
                tr.t_response = time.perf_counter()
                if tr.t_handler_in is not None:
                    rec.leave_handler()
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # 응답이 아예 안 나간 경우(연결 끊김 등)에도 인플라이트를 되돌린다.
            if not handler_left and tr.t_handler_in is not None:
                rec.leave_handler()
            tr.t_asgi_out = time.perf_counter()
            rec.commit(tr)
            _current.reset(token)


# --- 읽기 경로 --------------------------------------------------------------
#
# `/pose` 와 같은 인증(InternalAuthMiddleware)을 탄다. 공개 경로에 넣지 않는다 — 세션 수와
# 지연 분포는 운영 정보다.

router = APIRouter(prefix="/diag", tags=["진단"])


@router.get("/frame-path")
def read_frame_path() -> dict:
    """계측 스냅샷. 꺼져 있으면 `enabled: false` 만 돌려준다."""
    rec = get_recorder()
    if rec is None:
        return {"enabled": False}
    return {"enabled": True, **rec.snapshot()}


@router.post("/frame-path/reset")
def reset_frame_path() -> dict:
    """판 사이 초기화. 🔴 판마다 부르지 않으면 앞 판이 섞인다."""
    rec = get_recorder()
    if rec is None:
        return {"enabled": False, "reset": False}
    rec.reset()
    logger.info("프레임 경로 계측 초기화")
    return {"enabled": True, "reset": True}
