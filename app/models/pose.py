"""포즈 관련 Pydantic 모델."""

from enum import StrEnum

from pydantic import BaseModel, Field


class PoseSkipReason(StrEnum):
    """프레임이 **판정에 못 들어간** 사유 (이슈 #267).

    `message` 는 사람이 읽는 자리다. 프로그램이 보는 축이 따로 없으면 계약을 아는 쪽만
    안전하다 — 실제로 #196 통주행과 `e1_walkthrough.py` 가 각각 한 번씩 걸렸다.

    ⚠️ **두 부류가 섞여 있다.** 아래 주석의 «정상/비정상» 구분이 이 enum 의 존재 이유다 —
    `RATE_LIMITED` 는 서버가 의도적으로 자른 것이라 세션이 건강해도 나오고, 나머지는
    무언가 잘못됐다는 신호다. 이걸 `success` 한 축으로만 보면 둘을 못 가른다.
    """

    # 정상 동작 — 서버가 의도적으로 자른다
    RATE_LIMITED = "RATE_LIMITED"          # 유입 속도 상한 초과 (#143 ㄱ-2)

    # 입력 문제 — 프레임은 왔는데 쓸 수가 없다
    NO_POSE = "NO_POSE"                    # 사람을 못 찾았다
    LOW_VISIBILITY = "LOW_VISIBILITY"      # 관절 신뢰도 미달로 각도를 못 낸다

    # 세션 문제 — 순서·상태가 틀렸다
    NO_LEASE = "NO_LEASE"                  # 검출기 배정 없음 (StartAnalysis 미호출 또는 풀 상한)
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    UNSUPPORTED_EXERCISE = "UNSUPPORTED_EXERCISE"


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
    session_nonce: str | None = Field(
        default=None,
        description=(
            "세션 소유권 검증용 비밀값 (이슈 #187). 세션 시작·재부착·진행중조회 응답으로 "
            "Spring 이 내려준 값을 그대로 실어 보낸다. session_id 는 순차 정수라 추측되지만 "
            "이 값은 안 되므로, 남의 세션에 프레임을 꽂는 것을 이 대조가 막는다. "
            "1단계는 호환 모드다 — 안 보내면 검증을 건너뛴다."
        ),
    )
    timestamp_sec: float | None = Field(
        default=None,
        deprecated=True,
        description=(
            "[사용 안 함] 프레임 시각은 서버가 도착 시각으로 만든다 (이슈 #156). "
            "클라가 보내던 Date.now()/1000 은 epoch 라 «세션 시작 기준 경과 초» 가 아니었고, "
            "그 값이 변환 없이 리포트까지 흘러 시각 표시가 무의미해졌다. 호환을 위해 필드는 "
            "남겨두되 읽지 않는다 — 구버전 앱이 계속 보내도 무해하다."
        ),
    )


class PoseResponse(BaseModel):
    """포즈 감지 응답."""

    # 🔴 **«판정에 들어갔는가» 다** (이슈 #267 에서 의미를 좁혔다). «요청이 처리됐는가» 가 아니다.
    #
    # 예전에는 유입 상한 드롭과 가시성 부족 스킵이 `success=true` 로 나갔다. 그 둘은 landmarks
    # 를 담아 보내므로 «랜드마크가 왔다» 로 세면 30/31 처럼 보이는데 판정에 들어간 프레임은
    # 0 이었다 — #196 통주행이 그걸 「되고 있다」로 읽었다.
    #
    # 좁혀도 프론트는 안 깨진다(2026-08-20 실측): `exercise.tsx:179~188` 은 `sync_rate` 와
    # `rep_count` 만 읽고 `success` 를 안 본다. 스켈레톤 오버레이는 landmarks 로 그리는데
    # 그 필드는 스킵에서도 그대로 채운다 — 화면은 부드럽고 판정만 상한을 탄다.
    success: bool
    # success=False 일 때 «왜» 다. 성공이면 None.
    skip_reason: PoseSkipReason | None = None
    landmarks: list[Landmark] | None = None
    angles: list[float] | None = None
    message: str | None = None
    rep_count: int | None = None
    rep_completed: bool = False
    sync_rate: float | None = None
