"""포즈 관련 Pydantic 모델."""

from pydantic import BaseModel, Field


class Landmark(BaseModel):
    """MediaPipe 관절 랜드마크."""

    index: int
    x: float = Field(description="정규화된 x 좌표 (0~1)")
    y: float = Field(description="정규화된 y 좌표 (0~1)")
    z: float = Field(description="깊이 좌표")
    visibility: float = Field(description="감지 신뢰도 (0~1)")


class PoseRequest(BaseModel):
    """실시간 포즈 감지 요청."""

    image: str = Field(description="Base64 인코딩된 이미지")
    exercise_type: str = Field(
        default="squat", description="운동 유형 (squat, deadlift, pullup)"
    )
    session_id: int | None = Field(
        default=None,
        description="운동 세션 ID. 있으면 누적 분석 + rep 감지 시 Spring 콜백",
    )
    timestamp_sec: float | None = Field(
        default=None,
        description="영상 내 시간(초). 누락 시 서버 측 인덱스로 대체",
    )


class PoseResponse(BaseModel):
    """포즈 감지 응답."""

    success: bool
    landmarks: list[Landmark] | None = None
    angles: list[float] | None = None
    message: str | None = None
    rep_count: int | None = None
    rep_completed: bool = False
    sync_rate: float | None = None
