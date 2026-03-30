"""동기화율 관련 Pydantic 모델."""

from pydantic import BaseModel, Field


class SyncRequest(BaseModel):
    """동기화율 계산 요청."""

    reference_angles: list[list[float]] = Field(
        description="참고 영상의 관절 각도 시퀀스 [[angle1, angle2, ...], ...]"
    )
    user_angles: list[list[float]] = Field(
        description="사용자의 관절 각도 시퀀스 [[angle1, angle2, ...], ...]"
    )


class SyncResponse(BaseModel):
    """동기화율 계산 응답."""

    sync_rate: float = Field(description="동기화율 (0~100%)")
    dtw_distance: float = Field(description="정규화된 DTW 거리")
