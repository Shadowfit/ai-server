"""ShadowFit AI Server — FastAPI 진입점."""

import asyncio
import logging
import sys
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.config import settings
from app.core.mediapipe_detector import get_detector
from app.grpc.correlation import install_log_record_factory
from app.grpc.server import grpc_status, run_grpc_server, stop_grpc_server
from app.middleware.auth import InternalAuthMiddleware
from app.observability import frame_path, metrics

# basicConfig 보다 먼저 — 포맷의 %(cid)s 를 채울 속성을 LogRecord 에 주입하는 팩토리를 건다.
# Spring 로그의 [cid|sessionId] 와 같은 id 라서 두 서비스 로그를 한 줄기로 이어 읽을 수 있다.
install_log_record_factory()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(cid)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 시작 시 MediaPipe 모델 미리 로드
    get_detector()

    # gRPC 서버를 백그라운드 스레드로 함께 실행
    grpc_thread = threading.Thread(
        target=run_grpc_server, name="grpc-server", daemon=True
    )
    grpc_thread.start()
    logger.info("gRPC 서버 백그라운드 스레드 시작")

    # 스레드풀 상한·이벤트 루프 지체 샘플러. 🔴 limiter 는 RunVar 라 **루프 안에서** 띄워야
    # 같은 객체를 본다 — 그래서 여기다(모듈 최상단이 아니라).
    _pool_task = None
    if settings.FRAME_PATH_METRICS:
        _pool_task = asyncio.create_task(
            frame_path.sample_pool(frame_path.get_recorder()),
            name="frame-path-pool-sampler",
        )
        logger.warning("🔬 스레드풀·루프 샘플러 ON — 20ms 주기")

    if settings.POSE_NULL_HANDLER:
        # 🔴 서비스가 아무 일도 안 한다. 조용히 켜져 있으면 「빨라졌다」로 읽힌다.
        logger.warning("🔬 널 핸들러 팔 ON — POST /pose 가 즉시 반환한다 (측정 전용)")

    # GIL 지연 프로브(축 5). 🔴 **평범한 스레드**여야 한다 — 루프 태스크로 띄우면 루프
    # 지체와 같은 것을 재게 되어 둘의 차가 사라진다. 그 차가 이 프로브의 전부다.
    _gil_stop = None
    if settings.FRAME_PATH_METRICS and settings.GIL_PROBE_INTERVAL > 0:
        _gil_stop = threading.Event()
        threading.Thread(
            target=frame_path.probe_gil,
            args=(frame_path.get_recorder(), settings.GIL_PROBE_INTERVAL, _gil_stop),
            name="gil-probe",
            daemon=True,
        ).start()
    elif settings.GIL_PROBE_INTERVAL > 0:
        # 담을 자리가 없다. 조용히 넘어가면 「GIL 대기 0」 으로 읽힌다.
        logger.warning(
            "🔴 GIL_PROBE_INTERVAL 이 켜졌는데 FRAME_PATH_METRICS 가 꺼져 있다 — 프로브 안 뜬다"
        )

    yield

    if _pool_task is not None:
        _pool_task.cancel()
    if _gil_stop is not None:
        _gil_stop.set()

    # 종료 시 gRPC 서버 graceful stop
    stop_grpc_server()

    # 검출기 풀 정리 (#164). 프로세스가 죽으면 OS 가 회수하므로 누수는 아니지만, **몇 개가
    # 남아 있었는지가 로그에 남는다** — 무중단 배포가 없는 지금 그 숫자가 곧 「배포 때 몇 명이
    # 끊겼나」 다. 세션 상태 자체는 여전히 메모리에만 있어 복구되지 않는다(outbox §3-2).
    from app.core.mediapipe_detector import get_pool

    try:
        left = get_pool().shutdown()
        if left:
            logger.warning("종료 시 활성 세션 %d 개가 끊겼다 — 검출기 정리 완료", left)
        else:
            logger.info("종료 — 활성 세션 없음")
    except Exception as e:                          # 풀이 아직 안 만들어졌을 수 있다
        logger.info("검출기 풀 정리 생략: %s", e)


# DEBUG=false 는 두 compose 파일(dev·prod) 모두의 기본값이다 — 즉 컨테이너로 뜨는 이상
# 항상 꺼진다. venv로 직접 띄우는 로컬 개발(.env, DEBUG=true)에서만 열린다.
app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description="MediaPipe 포즈 감지, DTW 동기화율 계산, 영상 전처리 API",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
)

# allow_credentials 는 쿠키/TLS 클라이언트 인증서 전용이다 — 인증은 Bearer 토큰
# (InternalAuthMiddleware) 하나뿐이고 이 코드베이스 어디도 쿠키를 안 쓴다. True로 두면
# allow_origins=["*"] 와 조합될 때 Starlette이 요청의 Origin을 그대로 반사(reflect)해
# "자격증명 포함 요청을 아무 오리진에서나 허용"이 된다 — 필요 없는 값이라 뺀다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)
# 분기 H2 (프론트 → AI 직결) 대응 Bearer 토큰 검증.
# Starlette 는 나중에 add 한 미들웨어를 바깥쪽에 두므로 이 줄이 가장 바깥(요청 진입 시 첫 통과 지점)이 된다.
# OPTIONS preflight 와 /health 등 공개 경로는 미들웨어 내부에서 우회한다.
app.add_middleware(InternalAuthMiddleware)

# 프레임 경로 계측 (decisions/ai-receive-path-scaling.md §12). 기본은 꺼져 있다.
#
# 인증 미들웨어보다 **뒤에** add 한다 = 가장 바깥이다. 재려는 것이 «요청이 도착한 순간부터
# 워커에 실릴 때까지» 라, 인증·CORS 가 무는 시간도 그 구간 안에 들어와야 한다.
if settings.FRAME_PATH_METRICS:
    # 살아 있는 세션 수를 «스크레이프 시점에» 읽게 붙인다 (#151). 생성/삭제 자리마다 inc/dec 를
    # 심는 방식은 한 자리만 빠져도 조용히 어긋난다 — 레지스트리가 진실이다.
    from app.grpc.session_state import get_registry as _get_registry

    metrics.bind_active_sessions(lambda: _get_registry().active_count())

    _recorder = frame_path.install(settings.FRAME_PATH_SAMPLES)
    app.add_middleware(
        frame_path.FramePathMiddleware,
        recorder=_recorder,
        path=f"{api_router.prefix}/pose",
    )
    logger.warning(
        "🔬 프레임 경로 계측 ON — 표본 %d. 이 계측 자체가 GIL 을 잡는다. "
        "절대값을 인용하려면 계측 OFF 판과의 대조가 선행이다",
        settings.FRAME_PATH_SAMPLES,
    )

# 응답 생성 방식이 현행이 아니면 **조건에 남긴다**. 조용히 바뀌면 판이 무엇을 잰 건지 모른다.
if settings.RESPONSE_MODE != "model":
    logger.warning(
        "🔬 RESPONSE_MODE=%s — **응답 계약이 현행과 다르다.** 측정용 팔이고, "
        "이 판의 조건에 반드시 적을 것 (ai-process-ceiling-cause.md §11)",
        settings.RESPONSE_MODE,
    )

# GIL 스위치 간격을 바꾼다(후보 1순위 §10-1 을 흔드는 손잡이). 0 이면 안 건드린다.
if settings.GIL_SWITCH_INTERVAL > 0:
    sys.setswitchinterval(settings.GIL_SWITCH_INTERVAL)
    logger.warning(
        "🔬 sys.setswitchinterval(%.6f) — 기본값(0.005)이 아니다. 이 판의 조건에 적을 것",
        settings.GIL_SWITCH_INTERVAL,
    )

app.include_router(api_router)


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    """Prometheus 스크레이프 엔드포인트 (#151).

    🔴 **`/api/v1` 아래가 아니라 루트에 둔다.** 스크레이프 설정은 인프라가 쥐는 것이라
    앱의 버전 프리픽스를 따라다니면 안 된다 — Spring 의 관리 포트(`/actuator/prometheus`)와 같은 규약이다.

    인증은 안 건다(`PUBLIC_PATHS`). 안 그러면 Prometheus 가 401 을 받아 **영원히 DOWN 인 타깃**이
    되는데, 그건 prometheus.yml 이 «관측 스택이 고장난 것처럼 보인다» 고 경고한 바로 그 상태다.
    지표가 안 새는 근거는 인증이 아니라 **네트워크 경계**다(prod compose 가 이 포트를 호스트에 안 연다).
    """
    return Response(content=metrics.render(), media_type=metrics.CONTENT_TYPE)


@app.get("/health")
async def health_check():
    """gRPC 서버까지 살아 있어야 ok 다 (#430).

    🔴 **예전에는 무조건 ok 였다.** 그런데 gRPC 는 데몬 스레드에서 돌아 **스레드만 죽어도
    프로세스는 산다** — 그때 컨테이너는 `healthy`, HTTP 는 200, 그런데 Spring 은
    `Connection refused` 를 받고 세션이 전부 FAILED 로 떨어진다. 겉으로 드러나는 얼굴이
    원인과 안 닮아서 진단이 오래 걸린다(2026-08-23 에 세 판을 태웠다).

    compose 의 healthcheck 가 이 엔드포인트를 보므로, 503 을 내면 컨테이너가 `unhealthy` 로
    보인다 — 그게 이 변경이 노리는 전부다.

    ⚠️ **프로세스를 죽이지는 않는다.** `config.py` 의 `_assert_tokens_separated` 가
    *"로컬·테스트가 토큰 없이 도는 경로가 있다"* 고 적어둔 대로, 그 경로를 깨지 않는다.
    없애는 것은 «거짓 healthy» 뿐이다.

    ⚠️ 이 검사는 여전히 **얕다** — 검출기 풀은 안 건드린다(#214, `loadtest/aws/bootstrap.sh`).
    """
    serving, error = grpc_status()
    body = {
        "status": "ok" if serving else "degraded",
        "service": settings.APP_NAME,
        "grpc": {"serving": serving, "error": error},
    }
    if not serving:
        return JSONResponse(status_code=503, content=body)
    return body
