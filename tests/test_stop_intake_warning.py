"""StopAnalysis 가 «유입 경고» 세션에서 죽지 않는다 (이슈 #528 회귀).

무엇을 고정하나:
    `needs_intake_warning` 이 참인 세션(판정 프레임 0 · 가시성 스킵 있음)에서
    `StopAnalysis` 가 **예외 없이** 끝나고 **CompleteAnalysis 콜백을 띄우는지**.

왜 이 파일이 필요한가:
    #295(2026-08-21)가 그 경고 로그에 `judged` 라는 없는 이름을 써서 `NameError` 를 냈다.
    그 예외는 콜백 스레드 **앞에서** 터지므로, Spring 세션이 `end_time` 만 채워진 채
    `IN_PROGRESS` 로 영영 남고 사용자는 다음 세션을 못 연다(409). 3일간 안 잡힌 이유는
    **기존 테스트가 servicer 의 StopAnalysis 를 한 번도 부르지 않기 때문**이다 —
    `test_stop_idempotency.py` 는 스스로 «레지스트리만 본다, 두 곳이 갈리면 통과한 채 틀린다»
    고 적어 뒀고, 이번이 정확히 그 경우였다.

의도적으로 넓게 잡았다: 이 파일은 «판정 로그의 문구» 가 아니라 **«이 경로로 stop 이 통과하는가»**
를 지킨다. 문구는 바뀌어도 되지만 예외는 나면 안 된다.
"""

from __future__ import annotations

import threading

import exercise_pb2
from app.grpc import exercise_servicer as servicer_mod
from app.grpc.exercise_servicer import ExerciseServicer
from app.grpc.session_state import get_registry

_REF = [[90.0, 170.0], [80.0, 165.0]]


class _NoPool:
    """검출기 풀 대역. 🔴 진짜 풀은 «컨테이너 메모리 한도도 POSE_DETECTOR_POOL_SIZE 도 없으면»
    크기를 정하길 거부한다(`mediapipe_detector.get_pool`) — 근거 없는 기본값을 박지 않겠다는
    설계라 그 자체가 옳다. 그래서 **CI 의 맨 러너에서는 StopAnalysis 가 풀에서 먼저 죽는다.**
    이 파일이 보는 것은 풀이 아니라 stop 경로이므로 자리만 채운다."""

    def release(self, _session_id):
        return False


class _Ctx:
    """StopAnalysis 는 컨텍스트를 안 건드리지만, 시그니처를 채워야 부를 수 있다."""

    def set_code(self, *_args):  # pragma: no cover - 호출되면 그 자체가 실패 신호다
        raise AssertionError("StopAnalysis 가 오류 코드를 설정했다")

    def set_details(self, *_args):  # pragma: no cover
        pass

    def invocation_metadata(self):
        return ()


def _arm_callback(monkeypatch):
    """콜백을 가로챈다. 🔴 콜백은 **다른 스레드**에서 뜨므로 Event 로 기다려야 한다 —
    리스트만 보면 경합으로 «안 떴다» 가 되어 이 테스트가 제 손으로 거짓 실패를 만든다."""
    fired = threading.Event()
    seen = []

    def _fake(state):
        seen.append(state)
        fired.set()

    monkeypatch.setattr(servicer_mod, "_send_complete_analysis", _fake)
    monkeypatch.setattr(servicer_mod, "get_pool", lambda: _NoPool())
    return fired, seen


def _stop(session_id: int, monkeypatch) -> tuple:
    """세션 하나를 만들고 stop 을 부른다. 콜백은 실제로 안 보내고 «떴는지» 만 본다."""
    fired, seen = _arm_callback(monkeypatch)

    registry = get_registry()
    registry.create(
        session_id=session_id,
        exercise_id=1,
        reference_angles=_REF,
        persona="BEGINNER",
    )
    state = registry.get(session_id)
    response = ExerciseServicer().StopAnalysis(
        exercise_pb2.StopRequest(session_id=session_id), _Ctx()
    )
    return response, fired, state


def test_판정_0_세션의_stop_이_예외없이_끝난다(monkeypatch):
    """#528 의 본론 — 프레임이 한 장도 안 온 세션. 여기서 NameError 가 났다."""
    response, fired, state = _stop(528001, monkeypatch)

    assert state.needs_intake_warning is True, "이 테스트가 겨냥한 경로가 아니다"
    assert response.success is True
    # 콜백이 떠야 Spring 세션이 IN_PROGRESS 에서 빠져나온다. 이게 안 뜨는 것이 #528 의 피해다.
    assert fired.wait(5.0), "CompleteAnalysis 콜백이 뜨지 않았다 — 세션이 IN_PROGRESS 에 갇힌다"


def test_가시성_스킵만_있어도_stop_이_통과한다(monkeypatch):
    """`needs_intake_warning` 의 나머지 절반. 하체가 잠깐 프레임을 벗어나면 성립한다."""
    fired, _seen = _arm_callback(monkeypatch)

    registry = get_registry()
    registry.create(
        session_id=528002, exercise_id=1, reference_angles=_REF, persona="BEGINNER"
    )
    state = registry.get(528002)
    # 🔴 `judged_frame_count` 는 프로퍼티(수락 − 가시성스킵)라 직접 못 넣는다. 원재료를 넣는다.
    state.accepted_frame_count = 6
    state.visibility_skip_count = 1

    response = ExerciseServicer().StopAnalysis(
        exercise_pb2.StopRequest(session_id=528002), _Ctx()
    )

    assert state.judged_frame_count == 5, "판정 0 아닌 «스킵만 있는» 경로여야 한다"
    assert state.needs_intake_warning is True
    assert response.success is True
    assert fired.wait(5.0)
