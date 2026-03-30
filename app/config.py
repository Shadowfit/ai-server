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

    # Spring Boot 백엔드 URL (전처리 결과 저장용)
    BACKEND_URL: str = "http://localhost:8080/api/v1"

    # CORS 허용 출처
    CORS_ORIGINS: list[str] = ["*"]

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
