"""영상 전처리 API — 참고 영상에서 관절 데이터를 미리 추출."""

from fastapi import APIRouter, File, Form, UploadFile

from app.core.video_processor import analyze_video_bytes
from app.models.video import VideoAnalysisResult

router = APIRouter(prefix="/video", tags=["영상 전처리"])


@router.post("/analyze", response_model=VideoAnalysisResult)
async def analyze_uploaded_video(
    file: UploadFile = File(description="운동 참고 영상 파일 (.mp4)"),
    exercise_type: str = Form(
        default="squat", description="운동 유형 (squat, deadlift, pullup)"
    ),
):
    """업로드된 영상에서 프레임별 관절 좌표와 각도를 추출한다.

    참고 영상을 사전에 분석하여 결과를 저장해두면,
    실시간 운동 시 DTW 비교를 위한 기준 데이터로 사용할 수 있다.
    """
    video_bytes = await file.read()
    result = analyze_video_bytes(video_bytes, exercise_type)
    return result
