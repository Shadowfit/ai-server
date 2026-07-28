"""correlation id(요청 상관관계 식별자) 전파.

Spring 이 gRPC metadata 로 실어보낸 id 를 받아 이 프로세스의 로그에도 찍고, 반대로 Spring 으로
콜백할 때 다시 실어보낸다. 그래야 "폰 요청 → Spring → FastAPI → 콜백 → Spring" 전체가 로그에서
하나의 id 로 이어진다. Spring 쪽 대응 구현은 `global/observability/CorrelationIds.java`.

[스레드 주의] grpc 파이썬은 인터셉터가 도는 스레드와 핸들러가 도는 스레드가 다를 수 있다.
ContextVar 는 스레드마다 독립이므로 인터셉터에서 set 하면 핸들러가 못 본다 — 그래서 아래
CorrelationServerInterceptor 는 값을 세팅하지 않고 **핸들러 함수 자체를 감싸서** 핸들러가
실행되는 그 스레드 안에서 set/reset 한다.

같은 이유로 `threading.Thread` 는 부모의 ContextVar 를 **상속하지 않는다**(상속하려면
`contextvars.copy_context()` 가 필요). 핸들러가 백그라운드 스레드로 콜백을 띄울 때 그냥
넘기면 그 스레드의 id 는 빈 값이라, 콜백이 자기를 촉발한 요청과 이어지지 않는다. 그래서
스레드로 넘기는 함수는 `wrap()` 으로 감싼다 (Spring 의 `CorrelationIds.wrap(Runnable)` 대응).
"""

from __future__ import annotations

import functools
import logging
import re
import uuid
from contextvars import ContextVar

import grpc

# gRPC metadata 키는 규약상 소문자여야 한다 (Spring 의 CorrelationIds.GRPC_HEADER 와 동일 키).
METADATA_KEY = "x-request-id"

# 외부에서 들어온 id 를 그대로 로그에 찍으면 개행을 섞어 가짜 로그 줄을 만들 수 있다(로그 인젝션).
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")

_FALLBACK = "·"


def new_correlation_id(prefix: str = "ai") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def sanitize(raw: str | None) -> str | None:
    """부적합하면 None — 호출측이 새로 발급하면 된다."""
    if not raw:
        return None
    trimmed = raw.strip()
    return trimmed if _SAFE_ID.match(trimmed) else None


def get_correlation_id() -> str:
    return _correlation_id.get()


def ensure_correlation_id(prefix: str = "ai") -> str:
    """컨텍스트에 id 가 있으면 그대로, 없으면 새로 발급해 **컨텍스트에 심고** 반환한다.

    발급만 하고 버리지 않는 이유: 그러면 나가는 metadata 에는 id 가 붙는데 정작 같은 흐름이
    남기는 로그에는 `·` 가 찍혀 둘을 이을 수 없고, 같은 작업을 재시도할 때마다 서로 다른
    id 가 발급돼 한 작업의 시도들끼리도 안 묶인다.
    """
    current = _correlation_id.get()
    if current:
        return current
    generated = new_correlation_id(prefix)
    _correlation_id.set(generated)
    return generated


def correlation_metadata() -> tuple[tuple[str, str], ...]:
    """Spring 으로 나가는 호출에 붙일 metadata.

    현재 컨텍스트에 id 가 없으면(백그라운드 스레드에서 시작된 콜백 등) 새로 발급한다 —
    아무것도 못 잇는 것보다는 "AI 에서 시작된 흐름"이라는 id 라도 있는 편이 낫다.
    """
    return ((METADATA_KEY, ensure_correlation_id()),)


def wrap(fn):
    """스레드 경계로 넘길 함수를 감싼다 — **호출 시점**의 id 를 캡처해 **실행 시점**에 복원.

    `threading.Thread(target=...)` 는 부모 컨텍스트를 상속하지 않으므로, 감싸지 않으면
    백그라운드 콜백이 자기를 촉발한 요청과 이어지지 않는다. 캡처를 여기(부모 스레드)서
    하는 게 핵심 — 실행 시점에 읽으면 이미 남의 스레드라 항상 비어 있다.

    워커 스레드가 재사용될 수 있으므로 `finally` 로 반드시 원복한다.
    """
    captured = _correlation_id.get()

    @functools.wraps(fn)
    def runner(*args, **kwargs):
        token = _correlation_id.set(captured)
        try:
            return fn(*args, **kwargs)
        finally:
            _correlation_id.reset(token)

    return runner


class CorrelationServerInterceptor(grpc.ServerInterceptor):
    """[Spring → AI] metadata 의 correlation id 를 핸들러 실행 컨텍스트에 심는다."""

    def intercept_service(self, continuation, handler_call_details):
        metadata = dict(handler_call_details.invocation_metadata or ())
        correlation_id = sanitize(metadata.get(METADATA_KEY)) or new_correlation_id("ai-in")

        handler = continuation(handler_call_details)
        if handler is None or handler.unary_unary is None:
            # 이 서버의 RPC 는 전부 unary_unary — 그 외 타입은 감싸지 않고 그대로 통과시킨다.
            return handler

        inner = handler.unary_unary

        def wrapper(request, context):
            token = _correlation_id.set(correlation_id)
            try:
                return inner(request, context)
            finally:
                _correlation_id.reset(token)

        return grpc.unary_unary_rpc_method_handler(
            wrapper,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )


def install_log_record_factory() -> None:
    """모든 LogRecord 에 `cid` 속성을 주입한다.

    이러면 기존 logger.info(...) 호출을 하나도 고치지 않고 포맷 문자열의 %(cid)s 만으로
    전체 로그에 id 가 붙는다 (Spring 의 MDC + %X{cid} 와 같은 발상).
    """
    base_factory = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = base_factory(*args, **kwargs)
        record.cid = get_correlation_id() or _FALLBACK
        return record

    logging.setLogRecordFactory(factory)
