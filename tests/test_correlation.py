"""correlation id 전파 단위 테스트.

무거운 의존성(MediaPipe·전체 앱) 없이 인터셉터/유틸만 단독 검증한다.
Spring 쪽 대응 테스트는 backend `GrpcCorrelationInterceptorTest`.
"""

from __future__ import annotations

import logging

import grpc

from app.grpc.correlation import (
    METADATA_KEY,
    CorrelationServerInterceptor,
    correlation_metadata,
    get_correlation_id,
    install_log_record_factory,
    sanitize,
)


class _HandlerCallDetails:
    def __init__(self, metadata):
        self.method = "/exercise.ExerciseService/StopAnalysis"
        self.invocation_metadata = metadata


def _continuation_capturing(sink):
    """핸들러 실행 중의 correlation id 를 sink 에 기록하는 가짜 continuation."""

    def behavior(request, context):
        sink.append(get_correlation_id())
        return "ok"

    def continuation(handler_call_details):
        return grpc.unary_unary_rpc_method_handler(behavior)

    return continuation


def test_sanitize_rejects_log_injection():
    assert sanitize("plain-id_1") == "plain-id_1"
    assert sanitize("  spaced-id  ") == "spaced-id"
    assert sanitize("evil\n2026-07-27 INFO 가짜 로그") is None
    assert sanitize("") is None
    assert sanitize("x" * 65) is None


def test_interceptor_exposes_inbound_id_during_handler():
    seen = []
    interceptor = CorrelationServerInterceptor()

    handler = interceptor.intercept_service(
        _continuation_capturing(seen),
        _HandlerCallDetails(((METADATA_KEY, "cid-from-spring"),)),
    )
    handler.unary_unary("request", None)

    # 핸들러가 실제로 도는 컨텍스트 안에서 id 가 보여야 한다
    assert seen == ["cid-from-spring"]
    # 빠져나온 뒤에는 원복 — 워커 스레드 재사용 시 다음 호출로 새어나가면 안 된다
    assert get_correlation_id() == ""


def test_interceptor_generates_id_when_absent():
    seen = []
    interceptor = CorrelationServerInterceptor()

    handler = interceptor.intercept_service(_continuation_capturing(seen), _HandlerCallDetails(()))
    handler.unary_unary("request", None)

    assert seen[0].startswith("ai-in-")


def test_interceptor_rejects_unsafe_inbound_id():
    seen = []
    interceptor = CorrelationServerInterceptor()

    handler = interceptor.intercept_service(
        _continuation_capturing(seen),
        _HandlerCallDetails(((METADATA_KEY, "bad\nid"),)),
    )
    handler.unary_unary("request", None)

    assert "\n" not in seen[0]
    assert seen[0].startswith("ai-in-")


def test_outgoing_metadata_always_carries_an_id():
    metadata = dict(correlation_metadata())
    assert metadata[METADATA_KEY]  # 컨텍스트가 비어도 새로 발급


def test_outgoing_metadata_reuses_inbound_id():
    """Spring → AI → (콜백) Spring 이 같은 id 로 이어지는지 — 두 서비스 로그를 잇는 핵심."""
    captured = []
    interceptor = CorrelationServerInterceptor()

    def continuation(handler_call_details):
        def behavior(request, context):
            captured.append(dict(correlation_metadata())[METADATA_KEY])
            return "ok"

        return grpc.unary_unary_rpc_method_handler(behavior)

    handler = interceptor.intercept_service(
        continuation, _HandlerCallDetails(((METADATA_KEY, "end-to-end-1"),))
    )
    handler.unary_unary("request", None)

    assert captured == ["end-to-end-1"]


def test_log_record_factory_injects_cid():
    install_log_record_factory()
    record = logging.getLogRecordFactory()("t", logging.INFO, "p", 1, "msg", None, None)
    assert hasattr(record, "cid")
