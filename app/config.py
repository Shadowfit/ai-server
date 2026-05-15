from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "ShadowFit AI Server"
    DEBUG: bool = True

    # MediaPipe 설정
    POSE_MODEL_COMPLEXITY: int = 1  # 0=lite, 1=full, 2=heavy
    POSE_MIN_DETECTION_CONFIDENCE: float = 0.5
    POSE_MIN_TRACKING_CONFIDENCE: float = 0.5

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

    # 내부 서비스 간 공유 비밀키 (Spring과 동일한 값이어야 함)
    INTERNAL_API_TOKEN: str = ""

    # Spring gRPC 서버 주소 (콜백 대상)
    BACKEND_GRPC_ADDRESS: str = "shadowfit-backend:6565"

    # FastAPI gRPC 서버 포트
    AI_GRPC_PORT: int = 8585

    # CORS 허용 출처
    CORS_ORIGINS: list[str] = ["*"]

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
