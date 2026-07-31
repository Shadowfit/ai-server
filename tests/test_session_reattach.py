"""세션 재부착 단위 테스트 (이슈 #59 2단계).

검증 대상은 SessionStateRegistry.create_if_absent 의 멱등 가드다. 무거운 의존성(MediaPipe·gRPC
서버)을 띄우지 않고 레지스트리만 단독으로 본다 — 재부착의 핵심 계약이 "살아있는 상태를 덮어쓰지
않는다"이고, 그 계약은 전부 이 클래스 안에 있다.

Spring 쪽 대응 테스트는 backend `SessionReattachTest`.
"""

from __future__ import annotations

import threading

from app.grpc.session_state import PerRepFrame, SessionStateRegistry

_REF = [[90.0, 170.0], [80.0, 165.0]]


def _registry_with_live_session(session_id: int = 1):
    """rep 이 진행 중인 살아있는 세션 하나를 담은 레지스트리."""
    registry = SessionStateRegistry()
    state = registry.create(
        session_id=session_id,
        exercise_id=1,
        reference_angles=_REF,
        persona="BEGINNER",
    )
    # 재부착이 덮어쓰면 안 되는 것들 — 진행 중이던 rep 과 분석기 내부 상태
    state.rep_count = 5
    state.rep_state = "bottom"
    state.frame_index = 300
    state.previous_smoothed_knee = 95.5
    state.recent_raw_knees = [95.0, 96.0, 95.5]
    state.current_rep_frames.append(
        PerRepFrame(timestamp_sec=10.0, joint_coordinates="[]", angles=[95.0])
    )
    return registry, state


def test_상태가_없으면_주입된_rep_카운트로_생성된다():
    registry = SessionStateRegistry()

    state, already_active = registry.create_if_absent(
        session_id=1,
        exercise_id=1,
        reference_angles=_REF,
        persona="BEGINNER",
        initial_rep_count=7,
    )

    assert already_active is False
    # AI 메모리가 증발해도 완료된 rep 은 Spring 의 pose_data 에 남아있다 — 그 값이 여기로 들어온다.
    assert state.rep_count == 7
    assert state.reference_angles == _REF
    # 분석기 내부 상태는 복원되지 않는다(§4-0). 초기값이어야 한다.
    assert state.rep_state == "waiting_for_standing"
    assert state.frame_index == 0
    assert state.recent_raw_knees == []


def test_상태가_살아있으면_아무것도_덮어쓰지_않는다():
    """중복 호출·네트워크 재시도로 진행 중이던 rep 이 날아가면 안 된다."""
    registry, original = _registry_with_live_session()

    state, already_active = registry.create_if_absent(
        session_id=1,
        exercise_id=1,
        reference_angles=_REF,
        persona="BEGINNER",
        initial_rep_count=3,  # DB 값은 뒤처져 있을 수 있다 — 이걸 믿으면 rep 이 줄어든다
    )

    assert already_active is True
    assert state is original
    assert state.rep_count == 5  # 주입값 3 으로 후퇴하지 않았다
    assert state.rep_state == "bottom"
    assert state.frame_index == 300
    assert state.previous_smoothed_knee == 95.5
    assert len(state.current_rep_frames) == 1


def test_create_는_여전히_덮어쓴다():
    """create 의 기존 동작은 그대로다 — 재부착만 다른 규칙을 쓴다는 것을 고정한다."""
    registry, _ = _registry_with_live_session()

    state = registry.create(
        session_id=1,
        exercise_id=1,
        reference_angles=_REF,
        persona="BEGINNER",
    )

    assert state.rep_count == 0
    assert state.current_rep_frames == []


def test_동시_재부착에서_하나만_생성한다():
    """확인과 생성이 한 Lock 안에 있는지 — 나뉘어 있으면 그 틈으로 덮어쓰기가 뚫린다."""
    registry = SessionStateRegistry()
    # timeout 필수 — 한 스레드가 create_if_absent 전에 죽으면 나머지가 영구 대기하고 join 도
    # 안 돌아온다. 그러면 테스트가 "실패"가 아니라 "hang" 이 되어 CI 가 멈춘다.
    barrier = threading.Barrier(8, timeout=5)
    results: list[tuple[int, bool]] = []
    lock = threading.Lock()

    def reattach() -> None:
        barrier.wait()
        state, already_active = registry.create_if_absent(
            session_id=1,
            exercise_id=1,
            reference_angles=_REF,
            persona="BEGINNER",
            initial_rep_count=4,
        )
        with lock:
            results.append((id(state), already_active))

    threads = [threading.Thread(target=reattach) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8
    # 생성은 정확히 한 번, 나머지는 전부 멱등 경로
    assert sum(1 for _, already in results if not already) == 1
    # 전부 같은 객체를 받았다 — 스레드마다 다른 상태를 붙들면 rep 집계가 갈린다
    assert len({state_id for state_id, _ in results}) == 1


def test_다른_세션은_서로_영향이_없다():
    registry, _ = _registry_with_live_session(session_id=1)

    state, already_active = registry.create_if_absent(
        session_id=2,
        exercise_id=1,
        reference_angles=_REF,
        persona="BEGINNER",
        initial_rep_count=2,
    )

    assert already_active is False
    assert state.rep_count == 2
    assert registry.get(1).rep_count == 5
