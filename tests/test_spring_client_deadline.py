"""AI → Spring 호출의 데드라인·재시도 계약 (#206 결함 A · #209).

이 테스트가 지키는 것 둘:

  ① **모든 호출에 timeout 이 실린다.** 없으면 grpc-python 기본값이 None(무한 대기)이고,
     그러면 「3회 시도 · 백오프」 계약이 성립하지 않는다 — Spring 이 hang 하면 첫 시도가
     영영 안 끝나 재시도 루프가 한 번도 안 돈다. #206 이 잡은 것이 정확히 그것이다.

  ② **재시도는 «다시 던져도 되는» 코드에서만 돈다.** 영구 실패(세션 소멸 등)를 세 번 더
     던지면 결과는 같고 워커만 붙잡힌다.

⚠️ 스텁을 가짜로 갈아끼워서 본다 — 실제 Spring 을 띄우지 않는다. 여기서 확인하는 것은
   «우리가 무엇을 넘기는가» 와 «어떤 코드에서 다시 던지는가» 이고, 상대의 동작이 아니다.
"""

from __future__ import annotations

import unittest
from unittest import mock

import grpc

from app.config import settings
from app.grpc import spring_client


class _FakeRpcError(grpc.RpcError):
    """code()/details() 만 흉내 내는 최소 오류 — grpc 는 실제로 이 둘로 분기한다."""

    def __init__(self, code: grpc.StatusCode) -> None:
        super().__init__()
        self._code = code

    def code(self) -> grpc.StatusCode:
        return self._code

    def details(self) -> str:
        return f"fake {self._code}"


class RetryableCodes(unittest.TestCase):
    def test_transient_codes_are_retried(self):
        for code in (
            grpc.StatusCode.ABORTED,            # 데드락 재시도 소진 (#276 ③)
            grpc.StatusCode.UNAVAILABLE,
            grpc.StatusCode.DEADLINE_EXCEEDED,  # ①이 새로 만들어내는 코드다
            grpc.StatusCode.RESOURCE_EXHAUSTED,
        ):
            with self.subTest(code=code):
                assert spring_client._is_retryable(_FakeRpcError(code)) is True

    def test_permanent_codes_are_not_retried(self):
        for code in (
            grpc.StatusCode.NOT_FOUND,          # 세션 소멸 — 세 번 더 던져도 안 돌아온다
            grpc.StatusCode.INVALID_ARGUMENT,
            grpc.StatusCode.PERMISSION_DENIED,
            grpc.StatusCode.UNAUTHENTICATED,
        ):
            with self.subTest(code=code):
                assert spring_client._is_retryable(_FakeRpcError(code)) is False

    def test_unknown_shape_falls_back_to_retry(self):
        """코드를 못 읽으면 보수적으로 재시도한다 — 기존 동작을 안 깎는다."""

        class _NoCode(grpc.RpcError):
            pass

        assert spring_client._is_retryable(_NoCode()) is True


class CallsCarryDeadline(unittest.TestCase):
    def _stub_with(self, recorder):
        stub = mock.MagicMock()
        stub.SavePoseDataBatch.side_effect = recorder
        stub.CompleteAnalysis.side_effect = recorder
        stub.ExtractReferenceData.side_effect = recorder
        return stub

    def test_every_call_passes_timeout(self):
        seen = []

        def record(_request, **kwargs):
            seen.append(kwargs.get("timeout"))
            return mock.MagicMock(success=True)

        with mock.patch.object(spring_client, "get_stub", return_value=self._stub_with(record)):
            spring_client.report_pose_data_batch(1, [])
            spring_client.report_complete_analysis(1, 10, 80.0)
            spring_client.send_reference_poses(1, [])

        assert len(seen) == 3, f"세 경로가 다 불려야 한다: {seen}"
        for t in seen:
            # 🔴 None 이면 무한 대기다 — #206 이 잡은 그 상태로 되돌아간 것이다.
            assert t == settings.BACKEND_GRPC_TIMEOUT_SECONDS, f"timeout 이 안 실렸다: {seen}"

    def test_permanent_failure_is_not_retried(self):
        calls = []

        def always_not_found(_request, **_kwargs):
            calls.append(1)
            raise _FakeRpcError(grpc.StatusCode.NOT_FOUND)

        with mock.patch.object(
            spring_client, "get_stub", return_value=self._stub_with(always_not_found)
        ):
            spring_client.report_pose_data_batch(1, [])

        assert len(calls) == 1, f"영구 실패는 한 번만 던져야 한다 (실제 {len(calls)}회)"


if __name__ == "__main__":
    unittest.main()
