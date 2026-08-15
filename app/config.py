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

    # DTW 설정
    DTW_WINDOW_SIZE: int = 10  # Sakoe-Chiba band 크기

    # 영상 전처리 설정
    VIDEO_MAX_FPS: int = 30
    VIDEO_PROCESS_FPS: int = 10  # 분석 시 초당 프레임 수
    SQUAT_ROI_MIN_X: float = 0.38
    SQUAT_ROI_MIN_Y: float = 0.40
    SQUAT_ROI_MAX_X: float = 0.78
    SQUAT_ROI_MAX_Y: float = 0.76

    # Spring Boot 백엔드 URL (전처리 결과 저장용)
    BACKEND_URL: str = "http://localhost:8080/api/v1"

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

    # FastAPI gRPC 서버 포트
    AI_GRPC_PORT: int = 8585

    # CORS 허용 출처
    CORS_ORIGINS: list[str] = ["*"]

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()


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
