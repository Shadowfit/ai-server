"""DTW 동기화율 계산 API."""

from fastapi import APIRouter

from app.core.dtw_calculator import compute_dtw_distance, compute_sync_rate
from app.models.sync import SyncRequest, SyncResponse

router = APIRouter(prefix="/sync", tags=["동기화율"])


@router.post("", response_model=SyncResponse)
async def calculate_sync_rate(req: SyncRequest):
    """참고 영상과 사용자의 관절 각도 시퀀스를 비교하여 동기화율을 계산한다.

    프론트엔드에서 운동 세션 중 수집한 각도 데이터를
    참고 영상의 사전 분석 데이터와 비교하여 유사도를 반환한다.
    """
    sync_rate = compute_sync_rate(req.reference_angles, req.user_angles)
    dtw_distance = compute_dtw_distance(req.reference_angles, req.user_angles)

    return SyncResponse(sync_rate=sync_rate, dtw_distance=dtw_distance)
