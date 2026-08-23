"""`/health` 가 gRPC 서버 상태를 반영하는지 (#430).

예전에는 무조건 `{"status": "ok"}` 였다. gRPC 는 데몬 스레드에서 돌아 **스레드만 죽어도
프로세스는 살기** 때문에, 그 상태로 컨테이너가 계속 `healthy` 였다 — Spring 은
`Connection refused` 를 받는데 겉으로는 아무 이상이 없는 형태다.

전체 앱(`app.main`)을 띄우면 MediaPipe 가 딸려 오므로, `test_auth_middleware.py` 의 관례대로
**검증 대상 핸들러만** 단독 앱에 붙인다.

⚠️ `unittest.TestCase` 로 쓴 것은 취향이 아니다 — CI 가 `python -m unittest discover` 로
돌리므로(`.github/workflows/ai-server-test.yml`), 평범한 `def test_...` 함수는 **수집되지
않는다**(import 만 되고 실행은 안 된다). 이 파일은 실제로 돌아야 값이 있다.
"""

from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.config import settings
from app.grpc import server as grpc_server


def _client() -> TestClient:
    app = FastAPI()

    # app.main 의 핸들러와 **같은 본문**이다. 앱을 통째로 띄우지 않기 위한 복제이므로,
    # 한쪽을 고치면 다른 쪽도 고쳐야 한다.
    @app.get("/health")
    async def health_check():
        serving, error = grpc_server.grpc_status()
        body = {
            "status": "ok" if serving else "degraded",
            "service": settings.APP_NAME,
            "grpc": {"serving": serving, "error": error},
        }
        if not serving:
            return JSONResponse(status_code=503, content=body)
        return body

    return TestClient(app, raise_server_exceptions=False)


class HealthReflectsGrpcTests(unittest.TestCase):
    """모듈 전역 상태를 만지므로 판마다 되돌린다 — 안 하면 판 순서가 결과를 바꾼다."""

    def setUp(self) -> None:
        self._before = (grpc_server._serving, grpc_server._start_error)

    def tearDown(self) -> None:
        grpc_server._serving, grpc_server._start_error = self._before

    def test_gRPC_가_서빙_중이면_200(self):
        grpc_server._serving = True
        grpc_server._start_error = None

        res = _client().get("/health")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "ok")
        self.assertTrue(res.json()["grpc"]["serving"])

    def test_gRPC_가_안_떴으면_503_이고_사유가_실린다(self):
        """이 판이 #430 의 본체다 — 프로세스는 멀쩡한데 gRPC 만 죽은 상태."""
        grpc_server._serving = False
        grpc_server._start_error = (
            "RuntimeError: INTERNAL_API_TOKEN 환경변수가 설정되지 않았습니다."
        )

        res = _client().get("/health")

        self.assertEqual(
            res.status_code, 503,
            "컨테이너 healthcheck 가 이 코드를 보고 unhealthy 로 만든다",
        )
        body = res.json()
        self.assertEqual(body["status"], "degraded")
        self.assertFalse(body["grpc"]["serving"])
        self.assertIn(
            "INTERNAL_API_TOKEN", body["grpc"]["error"] or "",
            "사유가 실려야 진단이 원인에 닿는다",
        )


class GrpcStartFailureIsRecordedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._token = settings.INTERNAL_API_TOKEN
        self._before = (grpc_server._serving, grpc_server._start_error)

    def tearDown(self) -> None:
        settings.INTERNAL_API_TOKEN = self._token
        grpc_server._serving, grpc_server._start_error = self._before

    def test_토큰이_비면_기동이_실패하고_사유가_남는다(self):
        """`run_grpc_server` 가 사유를 기록하는지 — 그 기록이 위 503 의 근거다."""
        settings.INTERNAL_API_TOKEN = ""
        grpc_server._serving = True      # 실패가 이 값을 내리는지 보려고 일부러 참으로 둔다
        grpc_server._start_error = None

        with self.assertRaises(RuntimeError):
            grpc_server.run_grpc_server()

        serving, error = grpc_server.grpc_status()
        self.assertFalse(serving)
        self.assertIn("INTERNAL_API_TOKEN", error or "")


if __name__ == "__main__":
    unittest.main()
