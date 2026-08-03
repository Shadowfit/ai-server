"""sessionId별 in-memory 운동 분석 상태.

운동 세션이 진행되는 동안 reference 각도 시퀀스, 누적된 user 프레임,
rep 카운터·상태를 thread-safe하게 관리한다. StartAnalysis에서 생성되고
StopAnalysis 또는 CompleteAnalysis 콜백 직후 제거된다.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field

from app.models.pose import Landmark

# rep 하나에 담길 수 있는 최대 프레임 수 (이슈 #91).
#
# rep 은 길어야 몇 초다. 값은 "일어날 수 있는 최대 fps × 넉넉한 rep 길이"로 잡는다 —
# 현재 클라는 ~3fps(frontend exercise.tsx intervalMs=330)지만 config 의
# VIDEO_PROCESS_FPS 는 10 이고 실시간 POST 빈도가 코드로 강제돼 있지 않아
# (ai-load-budget.md §4.1) 위로 열려 있다. 10fps × 6초 = 60.
#
# 오판 방향은 안전한 쪽이다. 크게 잡으면 rep 이 아닌 프레임이 조금 섞일 뿐이지만,
# 작게 잡으면 진짜 rep 의 앞부분이 잘려 대표 프레임 선택(가장 깊게 앉은 순간)이
# 하강 구간을 못 보게 된다.
MAX_REP_FRAMES = 60


@dataclass
class PerRepFrame:
    timestamp_sec: float
    joint_coordinates: str  # JSON 직렬화된 landmark
    angles: list[float]


@dataclass
class CompletedRep:
    rep_number: int
    sync_rate: float
    frames: list[PerRepFrame]
    feedback_message: str = ""


@dataclass
class SessionState:
    session_id: int
    exercise_id: int
    exercise_type: str = "squat"
    persona: str = "BEGINNER"
    reference_angles: list[list[float]] = field(default_factory=list)

    # 진행 중인 rep에 누적되는 프레임들.
    #
    # 상한을 두는 이유(이슈 #91): 이 버퍼는 rep이 완성될 때만 비워진다. rep 사이에 흘러든
    # 프레임 — 세트 사이 휴식, 세트 중간에 멈칫하는 순간 — 이 계속 쌓이다가 다음 rep의
    # 배치에 통째로 실려 나가고, 그 프레임들이 다음 rep의 rep_number를 달고 저장된다.
    # 상한을 두면 오래된 것부터 밀려나 rep 직전 프레임만 남는다. 그게 실제로 그 rep을
    # 이루는 프레임이다.
    #
    # 클라가 휴식 중 전송을 멈추면(#92) 평상시 유입은 사라지지만, 앱은 '예정된' 휴식만
    # 알기 때문에 예정 없는 멈춤에는 여전히 프레임이 흐른다. 그래서 이 상한은 #92와
    # 무관하게 필요하다 — 서버가 클라의 협조에 기대지 않는 하한이다.
    current_rep_frames: deque[PerRepFrame] = field(
        default_factory=lambda: deque(maxlen=MAX_REP_FRAMES)
    )

    # 분석기 내부 상태 (StreamingSquatAnalyzer가 관리)
    rep_count: int = 0
    rep_state: str = "waiting_for_standing"
    last_rep_frame_index: int = -10_000
    # bottom 상태에 진입한 프레임. rep 완성 시 "바닥에 얼마나 머물렀나"를 재는 데 쓴다 —
    # 앉아서 쉬는 것과 스쿼트를 가르는 축이다 (이슈 #93).
    bottom_entry_frame_index: int = 0
    frame_index: int = 0
    previous_smoothed_knee: float | None = None
    recent_raw_knees: list[float] = field(default_factory=list)

    # 완료된 rep 요약 (StopAnalysis 시 평균 계산용)
    completed_reps: list[CompletedRep] = field(default_factory=list)


class SessionStateRegistry:
    """sessionId → SessionState 매핑. 모든 접근은 Lock 하에 수행."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[int, SessionState] = {}

    def create(
        self,
        session_id: int,
        exercise_id: int,
        reference_angles: list[list[float]],
        exercise_type: str = "squat",
        persona: str = "BEGINNER",
        initial_rep_count: int = 0,
    ) -> SessionState:
        with self._lock:
            state = SessionState(
                session_id=session_id,
                exercise_id=exercise_id,
                exercise_type=exercise_type,
                persona=persona,
                reference_angles=reference_angles,
                rep_count=initial_rep_count,
            )
            self._sessions[session_id] = state
            return state

    def create_if_absent(
        self,
        session_id: int,
        exercise_id: int,
        reference_angles: list[list[float]],
        exercise_type: str = "squat",
        persona: str = "BEGINNER",
        initial_rep_count: int = 0,
    ) -> tuple[SessionState, bool]:
        """재부착 전용. 상태가 이미 있으면 **보존하고** 그대로 돌려준다.

        create() 는 같은 id 로 다시 부르면 기존 상태를 덮어쓴다. 재부착 경로에서 그러면 중복 호출·
        네트워크 재시도·빠른 이탈 후 복귀 때 진행 중이던 rep 과 스무딩 이력을 통째로 버리게 된다 —
        정작 재부착이 필요 없는 경우에 피해를 주는 셈이다.

        확인과 생성이 한 Lock 안에 있어야 한다. get() 후 create() 로 나누면 그 사이에 다른 요청이
        상태를 만들 수 있고, 그러면 덮어쓰기를 막으려던 가드가 그대로 뚫린다.

        Returns:
            (상태, already_active) — already_active 가 True 면 아무것도 만들지 않았다는 뜻이다.
        """
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                return existing, True

            state = SessionState(
                session_id=session_id,
                exercise_id=exercise_id,
                exercise_type=exercise_type,
                persona=persona,
                reference_angles=reference_angles,
                rep_count=initial_rep_count,
            )
            self._sessions[session_id] = state
            return state, False

    def get(self, session_id: int) -> SessionState | None:
        with self._lock:
            return self._sessions.get(session_id)

    def remove(self, session_id: int) -> SessionState | None:
        with self._lock:
            return self._sessions.pop(session_id, None)

    def exists(self, session_id: int) -> bool:
        with self._lock:
            return session_id in self._sessions


# 프로세스 전역 싱글톤
_registry = SessionStateRegistry()


def get_registry() -> SessionStateRegistry:
    return _registry