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

import asyncio
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
#
# 🔑 `post` 는 **섞인 통**이다 — 앱 후처리(스레드 안)와 FastAPI 응답 직렬화(**이벤트 루프**)가
#    한 칸에 들어 있다. 그래서 후보 ㄴ(단일 이벤트 루프)과 ㄷ(스레드풀 상한)이 안 갈렸다
#    (`ai-process-ceiling-cause.md` §2-4). 아래 둘로 가른다:
#
#   post_app   추론 완료 → 핸들러 반환   앱 후처리 (**스레드가 잡고 있는 구간**)
#   post_loop  핸들러 반환 → 응답 시작   스레드 반납 후 **이벤트 루프**가 쓴 시간
#
# ⚠️ `post` 는 **안 지운다** — R10-a 값과 비교가 되어야 하고, `post_app + post_loop = post` 가
#    그 자체로 검산이다.
SPANS = (
    "wait", "decode", "lease", "infer",
    "post", "post_app", "post_loop",
    "respond", "total",
)


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
    # 핸들러가 값을 돌려준 시각. 🔴 **여기서 스레드가 반납된다** — 이 뒤는 이벤트 루프다.
    t_handler_out: float | None = None
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

        # ── 스레드풀·이벤트 루프 관측 (샘플러가 채운다, 핫 경로 아님) ────────
        #
        # 🔑 후보 ㄷ(스레드풀 상한)을 **직접** 본다. `inflight_handler` 로는 못 본다 —
        #    그 카운터는 `http.response.start` 까지 세므로 **스레드를 반납한 요청도 포함**한다
        #    (`ai-process-ceiling-cause.md` §2-2).
        # ⚠️ **최대와 분포를 남긴다.** 0단계 프로브에서 대기가 p50 0 · 최대 39 로 오갔다 —
        #    중앙값만 보면 「대기 없음」으로 읽힌다(그 결과 §2).
        self.pool_total = 0            # limiter 상한 (anyio 기본 40)
        self.pool_waiting_max = 0
        self.pool_borrowed_max = 0
        self.pool_waiting: dict[int, int] = {}    # tasks_waiting 분포
        self.pool_borrowed: dict[int, int] = {}   # borrowed_tokens 분포
        self.pool_samples = 0
        # 이벤트 루프 지체 — 샘플러가 「자기가 늦게 깬 만큼」을 잰다(후보 ㄴ 보강).
        self.loop_lag_ms: list[float] = []
        self.loop_lag_max_ms = 0.0
        # GIL 지연 프로브 — 「일 없는 스레드」가 깨어나기까지 (후보 ㄱ).
        # `per-process-ceiling-cause.md` 축 5. 루프 지체와의 **차**가 요점이라 따로 담는다.
        self.gil_lag_ms: list[float] = []
        self.gil_lag_max_ms = 0.0
        self.gil_samples = 0
        self.gil_interval_ms = 0.0

    def record_pool(self, total: int, borrowed: int, waiting: int, lag_ms: float) -> None:
        """샘플러 전용. 요청 경로가 아니라 **주기 태스크**가 부른다."""
        with self._lock:
            self.pool_total = total
            self.pool_samples += 1
            if waiting > self.pool_waiting_max:
                self.pool_waiting_max = waiting
            if borrowed > self.pool_borrowed_max:
                self.pool_borrowed_max = borrowed
            self.pool_waiting[waiting] = self.pool_waiting.get(waiting, 0) + 1
            self.pool_borrowed[borrowed] = self.pool_borrowed.get(borrowed, 0) + 1
            if lag_ms > self.loop_lag_max_ms:
                self.loop_lag_max_ms = lag_ms
            if len(self.loop_lag_ms) < self._cap:
                self.loop_lag_ms.append(lag_ms)

    def record_gil(self, lag_ms: float, interval_sec: float) -> None:
        """GIL 프로브 전용. **평범한 스레드**가 부른다(루프도 워커도 아니다)."""
        with self._lock:
            self.gil_samples += 1
            self.gil_interval_ms = interval_sec * 1000.0
            if lag_ms > self.gil_lag_max_ms:
                self.gil_lag_max_ms = lag_ms
            if len(self.gil_lag_ms) < self._cap:
                self.gil_lag_ms.append(lag_ms)

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
                # 🔑 ㄷ(스레드풀 상한)을 직접 답하는 칸이다. `waiting_max` 가 0 이면
                #    상한은 안 걸려 있었고, 그러면 `wait` 은 전부 루프 디스패치다.
                "thread_pool": {
                    "total_tokens": self.pool_total,
                    "waiting_max": self.pool_waiting_max,
                    "borrowed_max": self.pool_borrowed_max,
                    "waiting": dict(sorted(self.pool_waiting.items())),
                    "borrowed": dict(sorted(self.pool_borrowed.items())),
                    "samples": self.pool_samples,
                },
            }
            lag = list(self.loop_lag_ms)
            lag_max = self.loop_lag_max_ms
            gil = list(self.gil_lag_ms)
            gil_max = self.gil_lag_max_ms
            gil_seen = self.gil_samples
            gil_iv = self.gil_interval_ms
        out["loop_lag"] = _describe(lag, len(lag))
        out["loop_lag"]["max_ms"] = round(lag_max, 3)
        # 🔴 `samples: 0` 이면 **프로브가 안 돈 것**이다 — 「GIL 대기 없음」이 아니다.
        out["gil_lag"] = _describe(gil, gil_seen)
        out["gil_lag"]["max_ms"] = round(gil_max, 3)
        out["gil_lag"]["interval_ms"] = round(gil_iv, 3)
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
            self.pool_waiting_max = 0
            self.pool_borrowed_max = 0
            self.pool_waiting = {}
            self.pool_borrowed = {}
            self.pool_samples = 0
            self.loop_lag_ms = []
            self.loop_lag_max_ms = 0.0
            self.gil_lag_ms = []
            self.gil_lag_max_ms = 0.0
            self.gil_samples = 0


def _spans_of(tr: FrameTrace) -> dict[str, float]:
    """찍힌 시각만으로 구간을 만든다(ms). 조기 반환이면 뒷 구간이 통째로 빠진다."""
    pairs = (
        ("wait", tr.t_asgi_in, tr.t_handler_in),
        ("decode", tr.t_handler_in, tr.t_decoded),
        ("lease", tr.t_decoded, tr.t_leased),
        ("infer", tr.t_leased, tr.t_inferred),
        ("post", tr.t_inferred, tr.t_response),
        ("post_app", tr.t_inferred, tr.t_handler_out),
        ("post_loop", tr.t_handler_out, tr.t_response),
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


def mark_handler_out() -> None:
    """핸들러가 값을 돌려준 직후. **여기서 스레드가 반납되고 이후는 이벤트 루프다.**

    🔴 예외로 끝나도 찍혀야 짝이 맞는다 — 호출부가 `try/finally` 로 감싼다.
    """
    tr = _current.get()
    if tr is not None:
        tr.t_handler_out = time.perf_counter()


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

async def sample_pool(recorder: Recorder, interval: float = 0.02) -> None:
    """스레드풀 상한과 이벤트 루프 지체를 주기로 걷는다. **루프 안에서만 돈다.**

    🔴 limiter 는 `RunVar` 라 이벤트 루프 밖에서 읽으면 다른 객체가 나온다. 그래서 이 함수는
       `asyncio.create_task` 로만 띄운다(`main.py` lifespan).

    🔑 한 태스크가 둘을 잰다 — 어차피 `sleep` 하므로 **자기가 늦게 깬 만큼**이 곧 루프 지체다.
       재려고 따로 도는 것이 없어서 관측 비용이 하나로 묶인다.
    """
    import anyio.to_thread

    limiter = anyio.to_thread.current_default_thread_limiter()
    loop = asyncio.get_running_loop()
    while True:
        before = loop.time()
        await asyncio.sleep(interval)
        lag_ms = max(0.0, (loop.time() - before - interval) * 1000.0)
        st = limiter.statistics()
        recorder.record_pool(
            int(limiter.total_tokens),
            int(st.borrowed_tokens),
            int(st.tasks_waiting),
            lag_ms,
        )


def probe_gil(recorder: Recorder, interval: float, stop: threading.Event) -> None:
    """**일이 없는 평범한 스레드**에서 `sleep` 초과분을 잰다 (후보 ㄱ = GIL).

    설계: `docs/decisions/per-process-ceiling-cause.md` 축 5.

    🔑 왜 이게 GIL 을 가리키나 — 이 스레드는 깨어난 뒤 **아무 일도 안 한다.** 그래서
       초과분에 남는 것은 (ㄱ) OS 타이머 해상도 + 스케줄 지연과 (ㄴ) **GIL 재획득 대기**
       뿐이다. `sleep` 은 GIL 을 놓으므로, 다른 스레드가 GIL 을 오래 쥐면 그만큼 늦게 깬다.

    ⚠️ **루프 지체(`sample_pool`)와의 차가 요점이다.** 루프는 «자기 일» 로도 늦는다 —
       둘 다 늦으면 GIL, 루프만 늦으면 루프가 일이 많은 것이다. 한쪽만 보면 안 갈린다.

    🔴 **무부하 바닥을 먼저 재야 한다.** (ㄱ)은 부하와 무관하게 깔려 있고, 리눅스에서
       1ms `sleep` 은 놀아도 수십~수백 µs 늦는다. 그 값을 안 빼면 GIL 대기를 과대평가한다
       — 판마다 「부하 전 N초」를 같은 프로브로 걷고 그 차를 보는 것이 계약이다.

    🔑 **표본 수 자체가 두 번째 신호다.** 경합이 심하면 프로브가 굶어 덜 깬다 — 로컬 3초
       검증에서 무부하 2,133회(평균 0.394ms) 대 GIL 경합 131회(평균 23.194ms)였다. 기대
       표본(`구간초/interval`) 대비 모자란 만큼이 곧 「깨우지도 못했다」이므로, 평균만 보고
       `n` 을 안 보면 심한 경합을 오히려 작게 읽는다.

    ⚠️ 프로브 자신이 초당 `1/interval` 번 GIL 을 집는다. 그래서 이건 **별도 플래그**이고,
       프로브 ON/OFF 대조로 자기 몫을 뺄 수 있게 뒀다(`GIL_PROBE_INTERVAL`).
    """
    logger.info("🔬 GIL 지연 프로브 시작 — %.1fms 주기", interval * 1000.0)
    while not stop.is_set():
        t0 = time.perf_counter()
        time.sleep(interval)
        lag_ms = max(0.0, (time.perf_counter() - t0 - interval) * 1000.0)
        recorder.record_gil(lag_ms, interval)
    logger.info("🔬 GIL 지연 프로브 종료")

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
