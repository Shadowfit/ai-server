"""영상 전처리 API — 참고 영상에서 관절 데이터를 미리 추출.

🔴 저장소 안에 **호출자가 없다** (#293, 2026-08-22 확인).

**정본은 gRPC `ExtractReferenceData` 다**(`exercise_servicer.py`). 관리자가 유튜브 링크를
넣으면 Spring 이 AI 에 시키고 AI 가 `exercise_references` 로 되돌려준다(#192·#220).
이 HTTP 는 **파일 업로드판이고 결과를 아무 데도 저장하지 않는다** — JSON 으로 뱉고 끝이라
정답지가 될 수 없다. mp4 를 직접 올려 눈으로 확인하는 디버깅 수단으로는 쓸 수 있다.

**그래도 지우지 않는다** (2026-08-22 결정, #293) — 저장소 밖 소비자를 grep 으로 배제할 수
없어서 «표시만» 하기로 했다.
"""

from fastapi import APIRouter, File, Form, UploadFile

from app.core.video_processor import analyze_video_bytes
from app.models.video import VideoAnalysisResult

router = APIRouter(prefix="/video", tags=["영상 전처리"], deprecated=True)


@router.post("/analyze", response_model=VideoAnalysisResult)
async def analyze_uploaded_video(
    file: UploadFile = File(description="운동 참고 영상 파일 (.mp4)"),
    exercise_type: str = Form(
        default="squat", description="운동 유형 (squat, deadlift, pullup)"
    ),
):
    """업로드된 영상에서 프레임별 관절 좌표와 각도를 추출한다.

    ⚠️ 호출자 없음 (#293). 정본은 gRPC `ExtractReferenceData` 이고 그쪽만 결과를
    `exercise_references` 에 저장한다. 여기는 JSON 을 뱉고 끝이라 기준 데이터가 되지
    못한다 — 「사전에 분석하여 저장해두면」 은 이 경로로는 성립하지 않는다.
    """
    video_bytes = await file.read()
    result = analyze_video_bytes(video_bytes, exercise_type)
    return result
