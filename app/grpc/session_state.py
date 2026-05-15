"""sessionId별 in-memory 운동 분석 상태.

운동 세션이 진행되는 동안 reference 각도 시퀀스, 누적된 user 프레임,
rep 카운터·상태를 thread-safe하게 관리한다. StartAnalysis에서 생성되고
StopAnalysis 또는 CompleteAnalysis 콜백 직후 제거된다.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from app.models.pose import Landmark


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
    reference_angles: list[list[float]] = field(default_factory=list)

    # 진행 중인 rep에 누적되는 프레임들
    current_rep_frames: list[PerRepFrame] = field(default_factory=list)

    # 분석기 내부 상태 (StreamingSquatAnalyzer가 관리)
    rep_count: int = 0
    rep_state: str = "waiting_for_standing"
    last_rep_frame_index: int = -10_000
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
    ) -> SessionState:
        with self._lock:
            state = SessionState(
                session_id=session_id,
                exercise_id=exercise_id,
                exercise_type=exercise_type,
                reference_angles=reference_angles,
            )
            self._sessions[session_id] = state
            return state

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