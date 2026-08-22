"""DTW sync-rate API plus onboarding guidance.

🔴 저장소 안에 **호출자가 없다** (#293, 2026-08-22 확인).

- Spring 은 AI 를 gRPC 로만 부른다 — `backend/src` 전체에서 AI 로 가는 HTTP 호출 0건
- 프론트가 AI 를 부르는 곳은 `services/aiService.ts` 의 `POST /pose` 하나뿐
- `ai-server/tests` 도 이 라우트를 안 탄다 (core 모듈을 직접 부른다)

**실시간 싱크로율의 정본은 여기가 아니다.** `squat_analyzer.py` 가 `compute_sync_rate` 를
직접 부르고 그 값이 gRPC `SavePoseDataBatch` 로 간다 — 같은 함수를 부르는 두 입구인데
한쪽만 쓰인다.

**그래도 지우지 않는다** (2026-08-22 결정, #293). 저장소 밖 소비자를 grep 으로 배제할 수
없어서 «표시만» 하기로 했다. 지우기로 뒤집으면 `endpoints/video.py`, `api/router.py` 의
include 2줄, 문서 3곳이 같이 간다.

⭐ 지울 때도 `classify_sync_visual_cue` / `classify_sync_haptic_cue` 와
`SyncVisualCue` / `SyncHapticCue` 는 **남길 것** — gRPC 경로에는 UI 큐 개념이 아예 없고,
#193 이 채우려는 구멍이 정확히 그 자리다.
"""

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

router = APIRouter(prefix="/sync", tags=["sync"], deprecated=True)


@router.post("", response_model=SyncResponse)
async def calculate_sync_rate(req: SyncRequest) -> SyncResponse:
    """Compare reference and user angle sequences and return UI-ready cues.

    ⚠️ 호출자 없음 (#293). 상태가 없다 — 세션도 rep 도 모르고 넣은 두 배열만 비교한다.
    실시간 경로는 `squat_analyzer` → gRPC 를 쓴다.
    """
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
    """Return camera setup guidance for onboarding step 4.

    ⚠️ 호출자 없음 (#293). 프론트는 이걸 안 부르고 같은 내용을 자기 화면 두 곳에 직접
    박아뒀는데, 그 값이 이미 어긋나 있다(각도·거리) — 별건 #292.
    """
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
