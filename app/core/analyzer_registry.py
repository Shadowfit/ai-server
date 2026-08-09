"""분석기 레지스트리 — «이 종목을 분석할 수 있는가» 의 단일 출처.

이 모듈이 생긴 이유(이슈 #147): `StartAnalysis` 가 `exercise_type = "squat"` 을 **못박아**
넣고 있어서, 어떤 종목으로 세션을 시작하든 스쿼트 분석기가 돌았다. 런지를 켜면 세션은
정상으로 보이면서 **런지 동작이 스쿼트 기준으로 채점된다** — 에러가 아니라 조용히 틀린 점수라
발견이 늦는다.

거절 로직 자체는 원래도 두 곳에 있었다(`pose.py` 의 `미지원 운동`,
`angle_calculator.extract_angles` 의 `지원하지 않는 운동`). 도달을 못 했을 뿐이다. 이 모듈은
새 검사를 만드는 게 아니라 **진짜 값이 그 검사에 도달하게** 한다.

⚠️ **판정 기준을 `EXERCISE_ANGLES` 로 잡으면 안 된다.** 그건 각도 «정의» 표라 `deadlift`·
`pullup` 이 들어 있지만, rep 카운팅은 그 표를 타지 않는다 —
`squat_analyzer._extract_raw_metrics` 가 무릎 랜드마크를 하드코딩해 세기 때문이다. 각도 표를
기준으로 통과시키면 데드리프트가 무릎 각도로 rep 을 세는, 정확히 이 이슈가 없애려던 실패가
다시 생긴다. 그래서 기준은 **분석기 보유 여부**(`_ANALYZERS`)다.
"""

from __future__ import annotations

from app.core.squat_analyzer import StreamingSquatAnalyzer

# 운동 유형별 분석기 — stateless 클래스라 공유 가능.
# 여기 없는 유형은 «분석할 수 없다» 는 뜻이고, 그것이 이 dict 의 유일한 의미다.
_ANALYZERS: dict[str, StreamingSquatAnalyzer] = {
    "squat": StreamingSquatAnalyzer("squat"),
}

# exercises.id → 분석기 키.
#
# ⚠️ **DB 의 id 를 여기 적어둔 것이라 결합이 약하다.** 시드가 바뀌면(마이그레이션에서 id 를
#    다시 매기면) 이 표는 조용히 틀린다. 제대로 하려면 Spring 이 종목 코드를 실어 보내야
#    하는데(#147 ㄴ안 계열), `exercises` 에 코드 컬럼이 없고 `name` 이 한국어라 컬럼 추가
#    마이그레이션이 따라온다. 그건 별도 결정으로 두고, 지금은 표를 좁게 유지한다.
#
#    좁게 유지하는 것이 안전한 방향인 이유: 여기 없는 id 는 **거절**된다. 표가 낡으면
#    «되던 것이 안 되는» 쪽으로 틀리지, «안 될 것이 되는» 쪽으로 틀리지 않는다.
#
# 현재 시드(V2__seed_master_data.sql): 1=스쿼트 · 2=런지 · 3=플랭크.
# 런지·플랭크는 분석기가 없어 일부러 뺐다(squat-first).
_EXERCISE_ID_TO_TYPE: dict[int, str] = {
    1: "squat",
}


def resolve_exercise_type(exercise_id: int) -> str | None:
    """`exercise_id` 로 분석기 키를 찾는다. 분석할 수 없으면 `None`.

    매핑에 있어도 분석기가 없으면 `None` 을 준다 — 두 표가 어긋났을 때 «매핑에 있으니
    괜찮다» 로 통과시키지 않기 위해서다.
    """
    exercise_type = _EXERCISE_ID_TO_TYPE.get(exercise_id)
    if exercise_type is None or exercise_type not in _ANALYZERS:
        return None
    return exercise_type


def get_analyzer(exercise_type: str) -> StreamingSquatAnalyzer | None:
    """분석기를 꺼낸다. 없으면 `None`."""
    return _ANALYZERS.get(exercise_type)


def supported_exercise_ids() -> list[int]:
    """분석 가능한 `exercise_id` 목록 — 로그·에러 메시지용."""
    return sorted(
        exercise_id
        for exercise_id, exercise_type in _EXERCISE_ID_TO_TYPE.items()
        if exercise_type in _ANALYZERS
    )