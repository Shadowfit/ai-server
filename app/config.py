from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "ShadowFit AI Server"
    DEBUG: bool = True

    # MediaPipe 설정
    POSE_MODEL_COMPLEXITY: int = 1  # 0=lite, 1=full, 2=heavy
    POSE_MIN_DETECTION_CONFIDENCE: float = 0.5
    POSE_MIN_TRACKING_CONFIDENCE: float = 0.5

    # 검출기 풀 크기 = **동시 활성 세션 상한** (#164).
    #
    # 0 이면 컨테이너 메모리 한도에서 «유도» 한다 — 검출기 1개 = 98.7MB(M2 실측)이므로
    #   상한 = (cgroup 한도 − 기본 RSS 100.5MB) ÷ 98.7MB
    # 이 방식이면 환경(로컬/EC2)이 달라져도 값을 안 고쳐도 되고, 코드에 근거 없는 숫자가
    # 안 들어간다. 설정값을 주면 그 값을 쓰되 메모리 상한을 넘으면 낮춘다.
    #
    # ⚠️ 기본값을 «임의의 숫자» 로 두지 않은 것은 의도다([[feedback_no_arbitrary_threshold_values]]).
    #    한도도 없고 설정도 없으면 **기동을 거부한다.**
    POSE_DETECTOR_POOL_SIZE: int = 0

    # gRPC 서버 스레드풀 크기 (#593 — 근거 없는 매직넘버였다, 실측으로 좁히는 중).
    # 기본 10은 여전히 «측정된 적정값»이 아니라 배포 중인 값 그대로다 — 바뀌면 여기 주석도
    # 갱신할 것. loadtest/measure_grpc_threadpool_sizing_reattach.py --max-workers 로
    # 실측 스윕 시 이 값을 오버라이드한다.
    GRPC_MAX_WORKERS: int = 10

    # 프로세스 워커 수 (2026-08-26, GIL 병목 회피로 프로세스 분리 도입).
    #
    # 🔴 memory_ceiling() 이 컨테이너 메모리 한도를 «내가 유일한 프로세스» 라고 가정하고
    #    계산하던 문제(실측: 워커 3개 x POSE_DETECTOR_POOL_SIZE=160 = 약 47.4GB 시도,
    #    한도 20GB의 2.4배 오버부킹)를 막으려고 추가한다. entrypoint.sh 가 띄우는
    #    워커 수와 반드시 같은 값을 줘야 한다 — 어긋나면 이 계산 자체가 무의미해진다.
    AI_WORKER_COUNT: int = 1

    # DTW 설정
    DTW_WINDOW_SIZE: int = 10  # Sakoe-Chiba band 크기

    # 영상 전처리 설정
    VIDEO_MAX_FPS: int = 30
    VIDEO_PROCESS_FPS: int = 10  # 분석 시 초당 프레임 수
    SQUAT_ROI_MIN_X: float = 0.38
    SQUAT_ROI_MIN_Y: float = 0.40
    SQUAT_ROI_MAX_X: float = 0.78
    SQUAT_ROI_MAX_Y: float = 0.76

    # 내부 서비스 간 공유 비밀키 (Spring과 동일한 값이어야 함).
    # ⚠️ 이 값은 **서버 밖으로 나가지 않는다** — gRPC 양방향(Spring→AI 인터셉터,
    # AI→Spring 콜백)에만 쓴다. 클라이언트에 배포하면 안 된다 (이슈 #134).
    INTERNAL_API_TOKEN: str = ""

    # 프론트 → AI HTTP 직결(분기 H2) 전용 토큰. 앱 번들에 배포되는 값이다.
    #
    # INTERNAL_API_TOKEN 과 **값을 분리한 이유**(이슈 #134, decisions/ai-auth-token-flow.md ㄱ):
    # 예전엔 둘이 같은 값이라, 앱 번들에서 추출한 토큰으로 Spring 내부 gRPC(SavePoseDataBatch
    # 등 4개 RPC)까지 칠 수 있었다. 값을 나누면 유출 피해가 AI HTTP 로 한정된다.
    #
    # ⚠️ 이 토큰도 여전히 번들에 들어간다 — "누구나 /pose 를 호출할 수 있다" 는 그대로다.
    # 호출자 신원을 만드는 것은 별도 안(I2 세션 단기 토큰)이고 미결정이다.
    AI_PUBLIC_TOKEN: str = ""

    # Spring gRPC 서버 주소 (콜백 대상)
    BACKEND_GRPC_ADDRESS: str = "shadowfit-backend:6565"
    # 🔴 AI → Spring 호출의 데드라인(초). #206 결함 A — 이 값이 없던 동안 grpc-python 의
    #    기본값은 None(**무한 대기**)이었고, 그래서 「3회 시도 · worst-case 4초」라는 재시도
    #    계약이 성립하지 않았다: Spring 이 hang 하면 첫 시도가 영영 안 끝나 루프가 한 번도 안 돈다.
    #
    #    값 5초는 **새로 고른 숫자가 아니다** — 반대 방향(Spring → AI)이 같은 이유로 쓰는 값을
    #    그대로 복제한다(ExerciseAnalysisService.GRPC_CALL_TIMEOUT_SECONDS, "실측 튜닝된 값이
    #    아닌 보수적 기본값"). spring_client 가 재시도 횟수를 정할 때 쓴 방식과 같다.
    #
    #    ⚠️ 이건 «지연 예산» 이 아니라 **«hang 을 잡는 그물»** 이다. 앱 경로 실측에서 관측된
    #    p95 는 동거 조건에서 213ms 였다(loadtest/results/r276-backoff-sweep-aws-2026-08-23) —
    #    5초는 그 20배가 넘는다. 예산으로 쓰려면 따로 재고 따로 정해야 한다.
    BACKEND_GRPC_TIMEOUT_SECONDS: float = 5.0

    # FastAPI gRPC 서버 포트
    AI_GRPC_PORT: int = 8585

    # CORS 허용 출처
    CORS_ORIGINS: list[str] = ["*"]

    # --- 프레임 경로 계측 (decisions/ai-receive-path-scaling.md §12) ---
    #
    # 「346 RPS 천장에서 서버가 9.5 vCPU 만 쓰는 이유」를 앱 안에서 직접 재려고 붙였다.
    # py-spy 가 §11 에서 한계에 부딪혀 «앱에 붙이는 계측» 말고 방법이 없다.
    #
    # 🔴 **기본은 꺼져 있다.** 켜면 요청당 락 4회 + 타임스탬프 6회가 늘고, 그건 재려는 대상과
    #    같은 자원(GIL)이다. 상시로 두면 안 되는 종류의 계측이라 운영 기본값이 OFF 다.
    FRAME_PATH_METRICS: bool = False
    # 구간별로 남기는 표본 수(링). 넘으면 오래된 것부터 덮인다 — p99 는 «최근 N 개» 의 p99 다.
    FRAME_PATH_SAMPLES: int = 4096

    # GIL 스위치 간격(초). 0 이면 **안 건드린다**(파이썬 기본 0.005).
    #
    # 후보 1순위(서비스 경로 GIL, §10-1)를 직접 흔드는 노브다 — 이 값을 바꿔 처리량이
    # 움직이면 GIL 기여의 증거이고, 안 움직이면 1순위가 지워진다.
    # ⚠️ 값 자체에 «좋은 값» 은 없다. 팔을 만드는 손잡이지 튜닝 파라미터가 아니다.
    GIL_SWITCH_INTERVAL: float = 0.0

    # 응답 생성 방식(팔 손잡이). `ai-process-ceiling-cause.md` §11 이 쓰는 팔이다.
    #
    #   model  (기본, 현행)  response_model=PoseResponse  → Pydantic 검증 + 인코딩이 **루프**에서
    #   dict                 response_model 없음, dict 반환 → 검증이 빠지고 인코딩만 루프에서
    #   json                 JSONResponse 직접 반환        → 검증·인코딩이 **스레드**로 옮겨간다
    #
    # 🔴 **«비용을 없애는» 손잡이가 아니라 «어디서 쓰는지 옮기는» 손잡이다.** dict·json 은
    #    `model_dump()` 를 핸들러 안에서 부르므로 그 몫이 `post_app` 으로 **옮겨간다** —
    #    `post_loop` 가 줄고 `post_app` 이 그만큼 느는지가 이 판의 **검산**이다.
    #
    # 🔴 **dict·json 은 응답 계약을 바꾼다.** 측정용이지 채택안이 아니다 —
    #    운영에서 바꾸려면 프론트가 읽는 필드가 같은지부터 따로 확인해야 한다.
    # ⚠️ 모르는 값이면 기동을 막는다. 오타로 조용히 현행이 되면 팔이 사라진다.
    RESPONSE_MODE: str = "model"

    # GIL 지연 프로브 주기(초). 0 이면 **안 띄운다**. `per-process-ceiling-cause.md` 축 5.
    #
    # 일 없는 스레드가 이 주기로 자고 깨며 **늦게 깬 만큼**을 담는다. 깰 때 GIL 을 다시
    # 잡아야 하므로, 다른 스레드가 GIL 을 오래 쥐면 그 값이 커진다 — 후보 ㄱ 의 직접 계측이다.
    #
    # 🔴 **무부하 바닥을 먼저 재고 그 차를 봐야 한다.** OS 타이머 해상도가 깔려 있어서
    #    부하가 0 이어도 0 이 안 나온다. 절대값으로 「GIL 대기 N ms」 라고 읽으면 틀린다.
    # ⚠️ 프로브 자신이 초당 1/주기 번 GIL 을 집는다. 그래서 계측 플래그와 **따로** 두고,
    #    프로브 ON/OFF 대조로 자기 몫을 뺄 수 있게 했다.
    # 🔴 `FRAME_PATH_METRICS` 가 꺼져 있으면 담을 자리(recorder)가 없어 **안 뜬다.**
    GIL_PROBE_INTERVAL: float = 0.0

    # 널 핸들러 팔. 켜면 `POST /pose` 가 **본문만 받고 즉시** 응답한다
    # (디코딩·추론·분석·콜백 전부 없음). `per-process-ceiling-cause.md` 축 3.
    #
    # 🔑 재는 것은 «파이썬 계산을 다 뺀 뒤에도 프로세스당 천장이 남는가» 다. 남으면 천장은
    #    계산량이 아니라 **구조**(수신 경로·루프·GIL)이고, 사라지면 계산량이다.
    # 🔴 **응답이 가짜다.** 측정 전용이고 운영에서 켜면 서비스가 아무 일도 안 한다.
    POSE_NULL_HANDLER: bool = False

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()


def _assert_response_mode() -> None:
    """모르는 `RESPONSE_MODE` 로는 안 뜬다.

    🔴 오타가 조용히 «model»(현행)이 되면 **팔이 사라진 채로 판이 돈다** — 그 판은 세 팔이
    같은 것을 재고, 표는 정상으로 보인다. 그런 실패는 사후에 안 보이므로 여기서 막는다.
    """
    allowed = ("model", "dict", "json")
    if settings.RESPONSE_MODE not in allowed:
        raise RuntimeError(
            f"RESPONSE_MODE 가 {settings.RESPONSE_MODE!r} 다 — {allowed} 중 하나여야 한다"
        )


_assert_response_mode()


def _assert_tokens_separated() -> None:
    """두 토큰이 같으면 **기동을 거부한다** (#230).

    🔴 **주석만 있고 강제가 없었다.** 위 `AI_PUBLIC_TOKEN` 주석은 *"예전엔 둘이 같은 값이라,
       앱 번들에서 추출한 토큰으로 Spring 내부 gRPC 까지 칠 수 있었다"* 는 **실제 사고 이력**
       (#134)을 적어두고 있는데, 같은 값을 다시 넣는 것을 막는 코드는 없었다. compose 의
       `${...:?}` 는 **값의 존재만** 본다.

    즉 운영자가 «토큰 두 개 넣기 귀찮은데 같은 걸로» 하는 순간 #134 가 되돌아오고,
    **아무도 알려주지 않는다.**

    거부하는 쪽을 고른 이유는 이 프로젝트의 기존 규약과 같다 — 검출기 풀도 «근거가 없으면
    기본값을 박지 말고 기동을 거부» 한다(`mediapipe_detector.get_pool`). 조용히 도는 것보다
    안 뜨는 쪽이 낫다.

    ⚠️ **빈 값은 여기서 막지 않는다.** 로컬·테스트가 토큰 없이 도는 경로가 있고, 그건 이
    함수가 답할 질문이 아니다(운영 필수화는 compose 의 `:?` 가 맡는다).
    """
    a = settings.INTERNAL_API_TOKEN
    b = settings.AI_PUBLIC_TOKEN
    if a and b and a == b:
        raise RuntimeError(
            "🔴 INTERNAL_API_TOKEN 과 AI_PUBLIC_TOKEN 이 같다 (#230). "
            "AI_PUBLIC_TOKEN 은 앱 번들에 배포되는 값이라, 같은 값을 쓰면 번들에서 추출한 "
            "토큰으로 Spring 내부 gRPC 까지 통과한다 — 이슈 #134 가 그 사고였고 두 값을 "
            "나눈 이유다. 서로 다른 무작위 값을 넣을 것 (.env.example 참고)."
        )


_assert_tokens_separated()
