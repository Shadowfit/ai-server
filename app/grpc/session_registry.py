from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event, Lock, Thread

import exercise_pb2


@dataclass
class AnalysisSession:
    session_id: int
    exercise_id: int
    reference_source: str = ""
    stop_event: Event = field(default_factory=Event)
    thread: Thread | None = None
    status: int = exercise_pb2.SessionStatus.IN_PROGRESS
    pose_history: list[exercise_pb2.PoseDataRequest] = field(default_factory=list)
    final_result: exercise_pb2.SessionCompleteRequest | None = None


class SessionRegistry:
    """Thread-safe in-memory registry for long-running analysis sessions."""

    def __init__(self):
        self._lock = Lock()
        self._sessions: dict[int, AnalysisSession] = {}

    def create(self, session_id: int, exercise_id: int, reference_source: str = "") -> AnalysisSession:
        with self._lock:
            if session_id in self._sessions:
                raise ValueError(f"Session {session_id} is already active")
            session = AnalysisSession(
                session_id=session_id,
                exercise_id=exercise_id,
                reference_source=reference_source,
            )
            self._sessions[session_id] = session
            return session

    def get(self, session_id: int) -> AnalysisSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def set_thread(self, session_id: int, thread: Thread) -> None:
        with self._lock:
            session = self._sessions[session_id]
            session.thread = thread

    def append_pose_batch(self, session_id: int, pose_batch: list[exercise_pb2.PoseDataRequest]) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.pose_history.extend(pose_batch)

    def mark_final(self, session_id: int, final_result: exercise_pb2.SessionCompleteRequest, status: int) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.final_result = final_result
                session.status = status

    def stop(self, session_id: int) -> AnalysisSession | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.stop_event.set()
            return session

    def pop(self, session_id: int) -> AnalysisSession | None:
        with self._lock:
            return self._sessions.pop(session_id, None)
