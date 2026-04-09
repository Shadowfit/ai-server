"""Pydantic models for sync-rate responses."""

from pydantic import BaseModel, Field


class SyncRequest(BaseModel):
    """Request body for sync-rate calculation."""

    reference_angles: list[list[float]] = Field(
        description="Reference angle sequence [[angle1, angle2, ...], ...]"
    )
    user_angles: list[list[float]] = Field(
        description="User angle sequence [[angle1, angle2, ...], ...]"
    )


class SyncVisualCue(BaseModel):
    """UI cue derived from sync rate."""

    zone: str = Field(description="green, orange, or red")
    color: str = Field(description="Recommended UI color token")
    flashing: bool = Field(description="Whether the UI should blink")
    animation: str = Field(description="Recommended animation preset name")
    message: str = Field(description="Short text label for the UI")


class SyncHapticCue(BaseModel):
    """Haptic recommendation derived from sync rate."""

    enabled: bool = Field(description="Whether haptic feedback should trigger")
    pattern: str = Field(description="off, light_repeat, or warning_repeat")
    message: str = Field(description="Short explanation for the frontend")


class SyncResponse(BaseModel):
    """Response body for sync-rate calculation."""

    sync_rate: float = Field(description="Sync rate from 0 to 100")
    dtw_distance: float = Field(description="Normalized DTW distance")
    visual_cue: SyncVisualCue
    haptic_cue: SyncHapticCue


class OnboardingGuideItem(BaseModel):
    """Single onboarding tip for camera setup."""

    key: str = Field(description="Stable item identifier")
    title: str = Field(description="Short onboarding title")
    body: str = Field(description="User-facing guidance text")


class OnboardingGuideResponse(BaseModel):
    """Onboarding guide payload for frontend step 4."""

    step: int = Field(description="Onboarding step number")
    title: str = Field(description="Onboarding section title")
    items: list[OnboardingGuideItem]
