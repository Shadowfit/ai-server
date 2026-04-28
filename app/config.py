from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "ShadowFit AI Server"
    DEBUG: bool = True
    INTERNAL_API_TOKEN: str = "change-me"

    # MediaPipe settings
    POSE_MODEL_COMPLEXITY: int = 1
    POSE_MIN_DETECTION_CONFIDENCE: float = 0.5
    POSE_MIN_TRACKING_CONFIDENCE: float = 0.5

    # DTW settings
    DTW_WINDOW_SIZE: int = 10

    # Video processing settings
    VIDEO_MAX_FPS: int = 30
    VIDEO_PROCESS_FPS: int = 10
    SQUAT_ROI_MIN_X: float = 0.38
    SQUAT_ROI_MIN_Y: float = 0.40
    SQUAT_ROI_MAX_X: float = 0.78
    SQUAT_ROI_MAX_Y: float = 0.76

    # Backend integration settings
    BACKEND_URL: str = "http://localhost:8080/api/v1"
    SPRING_GRPC_HOST: str = "backend"
    SPRING_GRPC_PORT: int = 6565
    AI_GRPC_HOST: str = "0.0.0.0"
    AI_GRPC_PORT: int = 8585
    GRPC_CALLBACK_TIMEOUT_SEC: float = 3.0
    POSE_BATCH_SIZE: int = 20

    # CORS
    CORS_ORIGINS: list[str] = ["*"]

    model_config = {"env_file": ".env", "extra": "ignore"}

    @field_validator("DEBUG", mode="before")
    @classmethod
    def parse_debug(cls, value):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on", "debug", "development"}:
                return True
            if normalized in {"false", "0", "no", "off", "release", "production"}:
                return False
        return value


settings = Settings()
