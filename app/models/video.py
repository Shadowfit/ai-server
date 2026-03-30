"""영상 전처리 관련 Pydantic 모델."""

from pydantic import BaseModel, Field

from app.models.pose import Landmark


class FrameResult(BaseModel):
    """프레임 분석 결과."""

    frame_index: int
    timestamp: float = Field(description="타임스탬프 (초)")
    landmarks: list[Landmark]
    angles: list[float]


class VideoAnalysisResult(BaseModel):
    """영상 전체 분석 결과."""

    exercise_type: str
    total_frames: int
    analyzed_frames: int
    fps: float
    duration: float = Field(description="영상 길이 (초)")
    frames: list[FrameResult]


class VideoUploadRequest(BaseModel):
    """영상 분석 요청 (URL 방식)."""

    video_url: str = Field(description="영상 URL (YouTube 또는 직접 링크)")
    exercise_type: str = Field(
        default="squat", description="운동 유형 (squat, deadlift, pullup)"
    )
