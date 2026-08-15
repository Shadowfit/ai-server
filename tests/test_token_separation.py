"""두 토큰이 같으면 기동을 거부하는가 (#230).

🔴 **이 테스트가 지키는 것은 «코드» 가 아니라 «사고 이력» 이다.** #134 는 두 토큰이 같은
   값이라 앱 번들에서 추출한 토큰으로 Spring 내부 gRPC 까지 뚫린 사건이었다. 그때 값을
   나눴는데 **같은 값을 다시 넣는 것을 막는 코드가 없었다** — 주석만 있었다.
   이 테스트가 없으면 가드가 조용히 지워져도 아무도 모른다.

⚠️ **unittest 다.** 이 저장소에는 pytest 설정도 의존성도 없고 CI 가
   `python -m unittest discover -s tests` 로 돈다(`.github/workflows/ai-server-test.yml`).
   초판을 pytest 로 썼다가 CI 에서 `ModuleNotFoundError: pytest` 로 깨졌다.
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest


class TokenSeparationTest(unittest.TestCase):

    def setUp(self):
        self._saved = {k: os.environ.get(k)
                       for k in ("INTERNAL_API_TOKEN", "AI_PUBLIC_TOKEN")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # 다음 테스트가 이 모듈을 다시 읽도록 되돌린다 — 안 그러면 앞 판의 설정이 남는다.
        sys.modules.pop("app.config", None)

    @staticmethod
    def _load(internal: str, public: str):
        os.environ["INTERNAL_API_TOKEN"] = internal
        os.environ["AI_PUBLIC_TOKEN"] = public
        sys.modules.pop("app.config", None)
        return importlib.import_module("app.config")

    def test_같은_값이면_기동을_거부한다(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._load("SAME_SECRET", "SAME_SECRET")
        msg = str(ctx.exception)
        # 메시지에 «왜» 가 남아야 한다 — 운영자가 로그만 보고 고칠 수 있어야 하므로.
        self.assertIn("#230", msg)
        self.assertIn("번들", msg)

    def test_다른_값이면_통과한다(self):
        cfg = self._load("INTERNAL_SECRET", "PUBLIC_SECRET")
        self.assertNotEqual(cfg.settings.INTERNAL_API_TOKEN,
                            cfg.settings.AI_PUBLIC_TOKEN)

    def test_빈_값은_막지_않는다(self):
        """로컬·테스트가 토큰 없이 도는 경로가 있다. 운영 필수화는 compose 의 `:?` 가
        맡는다(#214) — 여기서 같이 막으면 두 관심사가 섞인다."""
        for internal, public in (("", ""), ("X", ""), ("", "X")):
            with self.subTest(internal=internal, public=public):
                self._load(internal, public)


if __name__ == "__main__":
    unittest.main()
