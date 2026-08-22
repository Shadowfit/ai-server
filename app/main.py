"""ShadowFit AI Server — FastAPI 진입점."""

import logging
import sys
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.config import settings
from app.core.mediapipe_detector import get_detector
from app.grpc.correlation import install_log_record_factory
from app.grpc.server import run_grpc_server, stop_grpc_server
from app.middleware.auth import InternalAuthMiddleware
from app.observability import frame_path

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

    yield

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


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description="MediaPipe 포즈 감지, DTW 동기화율 계산, 영상 전처리 API",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
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

# GIL 스위치 간격을 바꾼다(후보 1순위 §10-1 을 흔드는 손잡이). 0 이면 안 건드린다.
if settings.GIL_SWITCH_INTERVAL > 0:
    sys.setswitchinterval(settings.GIL_SWITCH_INTERVAL)
    logger.warning(
        "🔬 sys.setswitchinterval(%.6f) — 기본값(0.005)이 아니다. 이 판의 조건에 적을 것",
        settings.GIL_SWITCH_INTERVAL,
    )

app.include_router(api_router)


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": settings.APP_NAME}
