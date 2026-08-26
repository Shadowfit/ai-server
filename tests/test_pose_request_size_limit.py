"""`PoseRequest.image` 에 상한이 있는가.

이전에는 이 필드에 크기 제한이 전혀 없었다 — Pydantic 모델도, uvicorn 도, nginx-ai(prod 는
아예 없음, #552)도 막아주지 않았다. AI_PUBLIC_TOKEN 은 앱 번들에서 추출 가능하므로
(ai-auth-token-flow.md), 토큰만 있으면 임의 크기 페이로드를 반복 전송할 수 있었다.
"""
import unittest

from pydantic import ValidationError

from app.models.pose import PoseRequest


class PoseRequestImageSizeLimitTest(unittest.TestCase):
    def test_typical_frame_size_accepted(self):
        # loadtest/results/coresidency-2026-08-15/frames.json 실측 — jpeg_quality=80 합성
        # 프레임이 13~14KB 대. 실사용(quality=0.4)은 이보다 작을 것으로 예상되지만, 여기서는
        # "정상 크기는 통과해야 한다" 만 확인한다.
        PoseRequest(image="a" * 20_000)  # 예외 없이 생성되면 통과

    def test_oversized_payload_rejected(self):
        with self.assertRaises(ValidationError):
            PoseRequest(image="a" * 20_000_001)


if __name__ == "__main__":
    unittest.main()
