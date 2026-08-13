"""StopAnalysis 멱등성 단위 테스트 (이슈 #191).

검증 대상은 `SessionStateRegistry` 의 «종료 기록» 이다. 무거운 의존성(MediaPipe·gRPC 서버)을
띄우지 않고 레지스트리만 단독으로 본다 — 이 변경의 계약이 전부 이 클래스 안에 있기 때문이다.

무엇을 고정하나:
    아웃박스는 at-least-once 라 같은 StopAnalysis 가 두 번 올 수 있다(#152). 두 번째가 왔을 때
    응답이 «이미 처리했다»(가) 와 «세션을 정말 잃었다»(나) 를 갈라야 한다. 전에는 둘 다
    success=False 였고, 그래서 Spring 은 (가)를 지키려고 (나)의 빠른 실패를 포기하고 있었다.

⚠️ 이 파일이 고정하는 것은 **레지스트리의 판정**이고, 그 판정이 StopResponse.success 로
   옮겨지는 부분은 exercise_servicer.StopAnalysis 에 있다. 후자는 gRPC 스텁이 필요해
   여기서 다루지 않는다 — 두 곳이 갈리면 이 테스트는 통과한 채 동작만 틀릴 수 있다.

Spring 쪽 대응은 ExerciseAnalysisService 의 possiblyRedelivered 분기(#152).
"""

from __future__ import annotations

import threading

from app.grpc.session_state import (
    STOPPED_SESSION_RETENTION_SEC,
    SessionStateRegistry,
)

_REF = [[90.0, 170.0], [80.0, 165.0]]


def _registry_with_session(session_id: int = 1, retention_sec: float | None = None):
    kwargs = {} if retention_sec is None else {"retention_sec": retention_sec}
    registry = SessionStateRegistry(**kwargs)
    registry.create(
        session_id=session_id,
        exercise_id=1,
        reference_angles=_REF,
        persona="BEGINNER",
    )
    return registry


def test_첫_중단은_상태를_돌려준다():
    registry = _registry_with_session(1)

    state = registry.remove(1, now=100.0)

    assert state is not None
    assert state.session_id == 1


def test_두번째_중단은_상태가_없지만_최근_종료로_판정된다():
    """#191 의 본론 — 이게 (가) 와 (나) 를 가르는 지점이다."""
    registry = _registry_with_session(1)
    registry.remove(1, now=100.0)

    second = registry.remove(1, now=101.0)

    assert second is None, "상태는 첫 호출이 이미 가져갔다"
    assert registry.was_recently_stopped(1, now=101.0) is True, (
        "보유 기간 안이므로 «이미 처리됨» 으로 갈려야 한다"
    )


def test_한번도_없던_세션은_최근_종료가_아니다():
    """(나) 쪽 — 여기서 False 가 나와야 Spring 이 유실 세션을 감지한다.

    🔴 **같은 id 로 물어야 한다.** 처음엔 remove(999) 뒤에 was_recently_stopped(12345) 를
    봤는데, id 가 달라서 «remove 가 없는 세션까지 기록해버리는» 결함을 통째로 못 잡았다.
    StopAnalysis 는 remove() 한 그 id 를 그대로 다시 묻기 때문에, 테스트도 같아야 한다.
    """
    registry = SessionStateRegistry()

    assert registry.remove(999, now=100.0) is None
    assert registry.was_recently_stopped(999, now=100.0) is False, (
        "한 번도 없던 세션의 첫 중단은 «이미 처리됨» 이 아니다"
    )


def test_없는_세션을_지워도_기록이_남지_않는다():
    """CodeRabbit 이 PR #172 에서 잡은 결함의 회귀 테스트.

    remove() 가 상태 유무와 무관하게 기록하면 StopAnalysis 의 (나) 분기가 도달 불가가 되고,
    Spring 은 정말 유실된 세션을 SENT 로 종결한다 — 고치려던 것보다 나쁜 상태다.
    """
    registry = SessionStateRegistry()

    registry.remove(777, now=100.0)

    assert registry._recently_stopped == {}, (
        "꺼낸 상태가 없으면 종료 기록도 남기지 않아야 한다"
    )


def test_중복_중단이_보유_기간을_밀지_않는다():
    """재송신이 올 때마다 시각을 갱신하면 창이 무한정 밀린다."""
    registry = _registry_with_session(1)
    registry.remove(1, now=100.0)

    # 보유 기간이 끝나기 직전에 재송신이 한 번 더 온다
    registry.remove(1, now=100.0 + STOPPED_SESSION_RETENTION_SEC - 1)

    바깥 = 100.0 + STOPPED_SESSION_RETENTION_SEC + 1
    assert registry.was_recently_stopped(1, now=바깥) is False, (
        "최초 종료 시각 기준으로 만료돼야 한다"
    )


def test_보유_기간이_지나면_잊는다():
    registry = _registry_with_session(1)
    registry.remove(1, now=100.0)

    안쪽 = 100.0 + STOPPED_SESSION_RETENTION_SEC - 1
    바깥 = 100.0 + STOPPED_SESSION_RETENTION_SEC + 1

    assert registry.was_recently_stopped(1, now=안쪽) is True
    assert registry.was_recently_stopped(1, now=바깥) is False


def test_보유_기간_기본값은_Spring_회수_창을_덮는다():
    """값이 임의가 아니라 유도된 것임을 고정한다.

    lease 60s + 폴링 1s + gRPC 데드라인 5s = 66s. Spring 쪽 셋 중 하나가 바뀌면 이 테스트가
    아니라 **저쪽 설정**이 근거이므로, 여기 숫자를 고칠 때는 session_state.py 의 주석에 적힌
    세 값을 같이 확인해야 한다.
    """
    assert STOPPED_SESSION_RETENTION_SEC == 60.0 + 1.0 + 5.0


def test_읽기만_해도_만료분이_정리된다():
    """세션이 더 안 들어오는 동안에도 잊어야 한다.

    정리를 쓰기 경로에만 두면, remove() 가 다시 불리지 않는 한 만료된 id 가 남는다.
    """
    registry = _registry_with_session(1)
    registry.remove(1, now=100.0)

    registry.was_recently_stopped(1, now=100.0 + STOPPED_SESSION_RETENTION_SEC + 1)

    assert registry._recently_stopped == {}, "만료분이 남아 있으면 안 된다"


def test_동시_중단에서도_한_쪽만_상태를_가져간다():
    """CompleteAnalysis 콜백이 두 번 나가면 안 된다 — 그건 중복 결과 보고다."""
    registry = _registry_with_session(1)
    받은것: list[object] = []
    barrier = threading.Barrier(8)

    def 중단():
        barrier.wait()
        받은것.append(registry.remove(1, now=100.0))

    스레드 = [threading.Thread(target=중단) for _ in range(8)]
    for t in 스레드:
        t.start()
    for t in 스레드:
        t.join()

    상태받은수 = sum(1 for r in 받은것 if r is not None)
    assert 상태받은수 == 1, f"상태를 받은 호출이 {상태받은수}개 — 정확히 1개여야 한다"
    assert registry.was_recently_stopped(1, now=100.0) is True
