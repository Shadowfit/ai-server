"""Pydantic models for video analysis responses."""

from pydantic import BaseModel, Field

from app.models.pose import Landmark


class SquatFrameMetrics(BaseModel):
    """Derived squat metrics for a single analyzed frame."""

    knee_angle: float = Field(description="Average knee flexion angle in degrees")
    hip_angle: float = Field(description="Average hip angle in degrees")
    torso_tilt: float = Field(description="Torso tilt from vertical in degrees")
    hip_height: float = Field(description="Normalized hip height (0-1)")
    phase: str = Field(description="Estimated squat phase")
    cycle_stage: str = Field(default="waiting_for_standing", description="Squat repetition state")
    rep_count: int = Field(description="Completed squat repetitions so far")


class FrameResult(BaseModel):
    """Per-frame analysis result."""

    frame_index: int
    timestamp: float = Field(description="Frame timestamp in seconds")
    landmarks: list[Landmark]
    angles: list[float]
    squat_metrics: SquatFrameMetrics | None = None


class SquatAnalysisResult(BaseModel):
    """High-level squat analysis summary for a video."""

    reps_detected: int = Field(description="Number of completed squat repetitions")
    current_phase: str = Field(description="Current or final phase at the end of analysis")
    deepest_knee_angle: float = Field(description="Lowest average knee angle reached")
    mean_torso_tilt: float = Field(description="Average torso tilt from vertical")
    quality_score: int | None = Field(
        default=None, description="No validated scalar squat-quality score is available"
    )
    feedback: list[str] = Field(description="Human-readable coaching feedback")
    valid_frame_ratio: float = Field(description="Ratio of frames with reliable pose landmarks")


class VideoAnalysisResult(BaseModel):
    """Whole-video analysis result."""

    exercise_type: str
    total_frames: int
    analyzed_frames: int
    fps: float
    duration: float = Field(description="Video duration in seconds")
    frames: list[FrameResult]
    squat_analysis: SquatAnalysisResult | None = None


class VideoUploadRequest(BaseModel):
    """Video analysis request metadata for URL-based flows."""

    video_url: str = Field(description="Video URL")
    exercise_type: str = Field(
        default="squat", description="Exercise type (squat, deadlift, pullup)"
    )
