"""분석기 레지스트리 · 미지원 종목 거절 (이슈 #147).

검증 대상은 «이 종목을 분석할 수 있는가» 의 판정과, 못 할 때 두 진입점이 각각 어떻게
거절하는지다. MediaPipe·gRPC 서버를 띄우지 않고 servicer 메서드를 직접 부른다 — 판정과
거절 방식이 전부 그 안에 있다.

[왜 필요한가] 고치기 전에는 `exercise_type = "squat"` 이 못박혀 있어 **어떤 종목이든 통과**했고,
그 결과가 에러가 아니라 «조용히 틀린 점수» 였다. 실패로 보이지 않는 결함이라 회귀해도
아무도 모른다. 그래서 «거절된다» 자체를 고정한다.
"""

from __future__ import annotations

import grpc

import exercise_pb2
from app.core.analyzer_registry import (
    get_analyzer,
    resolve_exercise_type,
    supported_exercise_ids,
)
from app.grpc.exercise_servicer import ExerciseServicer

SQUAT_ID = 1
LUNGE_ID = 2  # 시드에는 있지만 분석기가 없다


class _FakeContext:
    """grpc.ServicerContext 대역 — abort 를 호출로 기록하고 예외로 끊는다.

    진짜 컨텍스트의 abort 도 예외를 던져 이후 코드가 실행되지 않는다. 그 성질을 흉내내지
    않으면 «abort 했는데 계속 진행» 하는 코드를 테스트가 통과시킨다.
    """

    def __init__(self) -> None:
        self.abort_code = None
        self.abort_details = None

    def abort(self, code, details):
        self.abort_code = code
        self.abort_details = details
        raise _Aborted(details)


class _Aborted(Exception):
    pass


def _assert_aborted(fn):
    """`fn()` 이 abort 로 끊기는지 확인한다.

    이 리포의 ai-server 테스트는 pytest 없이도 돌아야 해서(.venv 에 pytest 가 없다)
    `pytest.raises` 를 쓰지 않는다 — 나머지 11개 테스트 파일과 같은 규약이다.
    """
    try:
        fn()
    except _Aborted:
        return
    raise AssertionError("abort 로 끊기지 않았다 — 미지원 종목이 통과했다")


# ─── 레지스트리 ────────────────────────────────────────────────────────────────


def test_스쿼트는_분석기가_있다():
    assert resolve_exercise_type(SQUAT_ID) == "squat"
    assert get_analyzer("squat") is not None


def test_런지는_분석기가_없어_None():
    assert resolve_exercise_type(LUNGE_ID) is None


def test_매핑에_없는_id_는_None():
    assert resolve_exercise_type(9999) is None


def test_지원_목록에_스쿼트만_들어_있다():
    assert supported_exercise_ids() == [SQUAT_ID]


def test_각도_정의만_있는_종목은_통과하지_않는다():
    """EXERCISE_ANGLES 에는 deadlift·pullup 이 있지만 분석기는 없다.

    각도 표를 판정 기준으로 되돌리면 이 테스트가 깨진다 — 그게 이 테스트의 목적이다.
    rep 카운팅은 무릎 각도를 하드코딩해 세므로, 각도 정의가 있다고 통과시키면
    데드리프트가 무릎으로 rep 을 세는 조용한 오류가 다시 생긴다.
    """
    assert get_analyzer("deadlift") is None
    assert get_analyzer("pullup") is None


# ─── StartAnalysis — abort 로 거절 ──────────────────────────────────────────────


def test_StartAnalysis_는_미지원_종목을_abort_로_거절한다():
    """success=False 가 아니라 abort 여야 한다.

    Spring 의 onNext 는 success 를 읽지 않고 세션 id 만 로깅한다
    (ExerciseAnalysisService.java:244-247). success=False 로 돌려주면 그대로 삼켜져
    세션이 IN_PROGRESS 로 남는다. onError 경로여야 markAsFailedIfStillInProgress 가 돈다.
    """
    servicer = ExerciseServicer()
    context = _FakeContext()
    request = exercise_pb2.AnalyzeRequest(
        exercise_id=LUNGE_ID, session_id=1234, persona="BEGINNER"
    )

    _assert_aborted(lambda: servicer.StartAnalysis(request, context))

    assert context.abort_code == grpc.StatusCode.INVALID_ARGUMENT
    assert str(LUNGE_ID) in context.abort_details


def test_StartAnalysis_는_거절_시_세션_상태를_만들지_않는다():
    """거절해놓고 상태를 남기면 뒤이은 /pose 가 그 상태로 분석을 시작한다."""
    from app.grpc.session_state import get_registry

    servicer = ExerciseServicer()
    context = _FakeContext()
    session_id = 4321
    request = exercise_pb2.AnalyzeRequest(
        exercise_id=LUNGE_ID, session_id=session_id, persona="BEGINNER"
    )

    _assert_aborted(lambda: servicer.StartAnalysis(request, context))

    assert get_registry().get(session_id) is None


# ─── ReattachAnalysis — success=False 로 거절 ──────────────────────────────────


def test_ReattachAnalysis_는_미지원_종목을_success_False_로_거절한다():
    """여기는 abort 가 아니다.

    재부착은 Spring 이 응답을 실제로 읽어 W009 로 옮기는 경로가 이미 있다 — 바로 옆
    «기준 좌표 복원 실패» 분기와 같은 형태를 쓴다.
    """
    servicer = ExerciseServicer()
    context = _FakeContext()
    request = exercise_pb2.ReattachRequest(
        exercise_id=LUNGE_ID, session_id=555, initial_rep_count=3
    )

    response = servicer.ReattachAnalysis(request, context)

    assert response.success is False
    assert context.abort_code is None  # abort 로 끊지 않았다