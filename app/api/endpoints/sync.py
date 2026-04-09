"""DTW sync-rate API plus onboarding guidance."""

from fastapi import APIRouter

from app.core.dtw_calculator import (
    classify_sync_haptic_cue,
    classify_sync_visual_cue,
    compute_dtw_distance,
    compute_sync_rate,
)
from app.models.sync import (
    OnboardingGuideItem,
    OnboardingGuideResponse,
    SyncRequest,
    SyncResponse,
)

router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("", response_model=SyncResponse)
async def calculate_sync_rate(req: SyncRequest) -> SyncResponse:
    """Compare reference and user angle sequences and return UI-ready cues."""
    sync_rate = compute_sync_rate(req.reference_angles, req.user_angles)
    dtw_distance = compute_dtw_distance(req.reference_angles, req.user_angles)

    return SyncResponse(
        sync_rate=sync_rate,
        dtw_distance=dtw_distance,
        visual_cue=classify_sync_visual_cue(sync_rate),
        haptic_cue=classify_sync_haptic_cue(sync_rate),
    )


@router.get("/onboarding-guide", response_model=OnboardingGuideResponse)
async def get_onboarding_guide() -> OnboardingGuideResponse:
    """Return camera setup guidance for onboarding step 4."""
    return OnboardingGuideResponse(
        step=4,
        title="촬영 가이드",
        items=[
            OnboardingGuideItem(
                key="angle",
                title="각도",
                body="카메라는 몸 옆 90도 측면에 두고, 전신이 한 평면에서 보이게 촬영합니다.",
            ),
            OnboardingGuideItem(
                key="distance",
                title="거리",
                body="카메라와 2~3m 정도 거리를 두고 머리부터 발끝까지 화면 안에 모두 들어오게 맞춥니다.",
            ),
            OnboardingGuideItem(
                key="lighting",
                title="조명",
                body="역광을 피하고 정면 또는 측면에서 밝게 비춰 관절이 또렷하게 보이게 합니다.",
            ),
            OnboardingGuideItem(
                key="mirror",
                title="거울 주의",
                body="거울이나 반사체가 프레임에 들어오면 사람을 중복 인식할 수 있어 가능한 한 피합니다.",
            ),
        ],
    )
