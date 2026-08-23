"""AI 가 스크레이프 타깃으로 서는지 (#151).

이 이슈가 남긴 마지막 조각은 «AI 를 스크레이프 타깃으로» 하나였다. 그래서 이 테스트가 묻는 것도
세 가지뿐이다 — **긁을 수 있는가 · 인증에 막히지 않는가 · 못 세던 것을 세는가.**

⚠️ Prometheus 를 띄우지 않는다. 스크레이프 자체는 인프라의 몫이고, 여기서 지키는 것은
   **앱이 내놓는 쪽**이다.
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.observability import metrics


class MetricsEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_metrics_is_reachable_without_token(self):
        """🔴 인증을 요구하면 Prometheus 가 401 을 받아 «영원히 DOWN 인 타깃» 이 된다.

        prometheus.yml 이 ai-server 를 빼둔 이유가 정확히 그 상태를 피하려는 것이었다.
        """
        res = self.client.get("/metrics")  # Authorization 헤더 없음

        assert res.status_code == 200, f"인증에 막히면 타깃이 죽는다: {res.status_code}"
        assert "text/plain" in res.headers["content-type"]

    def test_exposes_process_metrics(self):
        """프로세스 지표는 prometheus_client 기본 컬렉터가 준다.

        이 이슈가 «아픈 건 아는데 어디가 아픈지 모른다» 로 적은 자리 — 서킷이 열렸을 때
        AI 가 메모리인지 CPU 인지 — 의 절반이 이것으로 열린다.
        """
        body = self.client.get("/metrics").text

        assert "python_info" in body or "process_resident_memory_bytes" in body, body[:200]

    def test_counts_callback_outcomes(self):
        """🔴 «두 겹이 다 소진돼 rep 이 사라진 사건» 이 이제 세어진다 (#276 ③ 이 남긴 조각).

        지금까지 그 사건은 ERROR 로그 한 줄로만 남아 «얼마나 자주 일어나나» 에 답이 없었다.
        """
        metrics.record_callback("SavePoseDataBatch", "exhausted")

        body = self.client.get("/metrics").text

        assert 'shadowfit_ai_spring_callback_total{outcome="exhausted",rpc="SavePoseDataBatch"}' in body, (
            "소진 카운터가 안 보인다 — 라벨 이름이 바뀌었는지 확인할 것:\n"
            + "\n".join(l for l in body.splitlines() if "spring_callback" in l)
        )

    def test_active_sessions_reads_the_registry(self):
        """세션 게이지는 «스크레이프 시점에» 레지스트리를 읽는다.

        생성/삭제 자리마다 inc/dec 를 심는 방식은 한 자리만 빠져도 조용히 어긋난다 —
        그래서 값을 세지 않고 **물어본다**. 이 테스트는 그 배선이 실제로 붙었는지만 본다.
        """
        body = self.client.get("/metrics").text

        assert "shadowfit_ai_active_sessions" in body


if __name__ == "__main__":
    unittest.main()
