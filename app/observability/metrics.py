"""ai-server 의 Prometheus 지표 (#151).

관측 스택이 세 서비스 중 **Spring 하나만** 덮고 있었다. MySQL 은 mysqld_exporter 로 닫혔고,
남은 것이 여기다 — 이 모듈이 그 자리를 연다.

🔴 **왜 «계측이 있다» 로 부족했나.** #384 가 프레임 경로 계측을 붙였지만 그 형태는
`frame_path.snapshot()` 딕셔너리 + `GET /frame-path` 다. 그건 **스냅샷**이고 스택이 긁는 것은
**시계열**이다. 이 이슈 자신이 mysqld_exporter 를 들일 때 쓴 잣대가 정확히 그것이었다 —
*"스냅샷과 시계열을 같은 것으로 본 판정이다"*. 같은 잣대를 AI 에 대면 아직 안 덮인 상태였다.

## 무엇을 세나 — 지금은 «못 세던 것» 부터

`shadowfit_ai_spring_callback_total{rpc, outcome}` 하나가 이 모듈의 본체다.

AI → Spring 콜백은 **두 겹의 재시도** 중 바깥 겹이다(안쪽은 Spring 의 데드락 재시도, #276).
두 겹이 다 소진되면 **rep 하나가 사라지는데**, 지금까지 그 사건은 ERROR 로그 한 줄로만 남아
«얼마나 자주 일어나나» 에 답이 없었다(#276 ③ 이 남긴 조각). outcome 을 갈라 세면 그 답이 생긴다:

    ok        성공 (재시도를 거쳤든 아니든)
    retried   다시 던졌다 (횟수만큼 오른다)
    rejected  영구 실패라 안 던졌다 (NOT_FOUND 등 — 세션이 사라진 뒤)
    exhausted 재시도 상한까지 갔는데 실패 ← 🔴 이 칸이 «유실» 이다

⚠️ **임계값은 여기서 정하지 않는다.** 「몇 건부터 나쁜가」는 `docs/decisions/slo-baseline.md`
가 미확정으로 두고 있고, 근거 없는 숫자를 계측 코드에 박지 않는다. 이 모듈이 답하는 것은
**세는 수단**까지다.

## 프로세스 지표는 공짜로 따라온다

`prometheus_client` 기본 컬렉터가 CPU·메모리·GC·FD 를 내보낸다. 이 이슈가 «아픈 건 아는데
어디가 아픈지 모른다» 로 적은 자리 — 서킷이 열렸을 때 AI 가 메모리인지 CPU 인지 —
의 절반이 그것으로 열린다.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest
from prometheus_client import REGISTRY as DEFAULT_REGISTRY

# 기본 레지스트리를 쓴다 — 프로세스 지표(CPU·메모리·GC)가 거기 붙어 있고, 그게 이 이슈가
# 원하던 절반이다. 별도 레지스트리를 만들면 그것들이 안 나온다.
REGISTRY: CollectorRegistry = DEFAULT_REGISTRY

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# rpc 라벨을 두는 이유: 세 콜백의 성격이 다르다. SavePoseDataBatch 는 rep 마다 오고
# CompleteAnalysis 는 세션당 한 번이라, 같은 «실패 1건» 의 무게가 다르다.
spring_callback_total = Counter(
    "shadowfit_ai_spring_callback",
    "AI → Spring 콜백 결과. exhausted 는 «두 겹이 다 소진돼 유실» 을 뜻한다 (#151, #276 ③)",
    ["rpc", "outcome"],
)

# 살아 있는 세션 수. 게이지를 «지금 값을 물어보는» 방식으로 두는 이유는, 세션 생성/삭제 자리마다
# inc/dec 를 심으면 한 자리만 빠져도 조용히 어긋나기 때문이다 — 레지스트리가 진실이다.
active_sessions = Gauge(
    "shadowfit_ai_active_sessions",
    "in-memory session_state 에 살아 있는 세션 수 (#151)",
)


def record_callback(rpc: str, outcome: str) -> None:
    """콜백 결과를 센다. 지표 때문에 콜백이 죽으면 안 되므로 절대 던지지 않는다."""
    try:
        spring_callback_total.labels(rpc=rpc, outcome=outcome).inc()
    except Exception:  # pragma: no cover - 계측이 본 경로를 깨뜨리지 않는다
        pass


def bind_active_sessions(count_fn) -> None:
    """세션 수를 «스크레이프 시점에» 읽도록 붙인다."""
    active_sessions.set_function(count_fn)


def render() -> bytes:
    """Prometheus 텍스트 포맷."""
    return generate_latest(REGISTRY)
