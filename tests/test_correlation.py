"""correlation id 전파 단위 테스트.

무거운 의존성(MediaPipe·전체 앱) 없이 인터셉터/유틸만 단독 검증한다.
Spring 쪽 대응 테스트는 backend `GrpcCorrelationInterceptorTest`.
"""

from __future__ import annotations

import logging
import threading

import grpc

from app.grpc.correlation import (
    METADATA_KEY,
    CorrelationServerInterceptor,
    correlation_metadata,
    get_correlation_id,
    install_log_record_factory,
    sanitize,
    wrap,
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
    # 전역 팩토리를 갈아끼우므로 반드시 원복한다 — pytest 없이 한 프로세스에서
    # 함수를 직접 호출해 돌리는 방식이라, 안 되돌리면 뒤에 실행되는 테스트가 영향받는다.
    original_factory = logging.getLogRecordFactory()
    try:
        install_log_record_factory()
        record = logging.getLogRecordFactory()("t", logging.INFO, "p", 1, "msg", None, None)
        assert hasattr(record, "cid")
    finally:
        logging.setLogRecordFactory(original_factory)


def test_wrap_carries_id_across_thread_boundary():
    """threading.Thread 는 ContextVar 를 상속하지 않는다 — wrap 이 그 구멍을 메우는지."""
    seen: list[str] = []
    interceptor = CorrelationServerInterceptor()

    def continuation(handler_call_details):
        def behavior(request, context):
            # 핸들러 안에서 백그라운드 콜백을 띄우는 실제 StopAnalysis 패턴 그대로
            thread = threading.Thread(target=wrap(lambda: seen.append(get_correlation_id())))
            thread.start()
            thread.join()
            return "ok"

        return grpc.unary_unary_rpc_method_handler(behavior)

    handler = interceptor.intercept_service(
        continuation, _HandlerCallDetails(((METADATA_KEY, "across-thread-1"),))
    )
    handler.unary_unary("request", None)

    assert seen == ["across-thread-1"]


def test_unwrapped_thread_loses_id():
    """wrap 이 없으면 실제로 끊긴다는 것 — 위 테스트가 무엇을 막고 있는지 고정한다."""
    seen: list[str] = []
    interceptor = CorrelationServerInterceptor()

    def continuation(handler_call_details):
        def behavior(request, context):
            thread = threading.Thread(target=lambda: seen.append(get_correlation_id()))
            thread.start()
            thread.join()
            return "ok"

        return grpc.unary_unary_rpc_method_handler(behavior)

    handler = interceptor.intercept_service(
        continuation, _HandlerCallDetails(((METADATA_KEY, "across-thread-2"),))
    )
    handler.unary_unary("request", None)

    assert seen == [""]


def test_metadata_is_stable_across_retries_in_background_thread():
    """컨텍스트가 빈 스레드에서도 한 작업의 재시도들이 같은 id 로 묶이는지.

    ensure_correlation_id 가 fallback id 를 컨텍스트에 심어두기 때문에 성립한다.
    """
    ids: list[str] = []

    def background():
        for _ in range(3):  # CompleteAnalysis 재시도 3회를 흉내
            ids.append(dict(correlation_metadata())[METADATA_KEY])

    thread = threading.Thread(target=background)
    thread.start()
    thread.join()

    assert ids[0].startswith("ai-")
    assert len(set(ids)) == 1
