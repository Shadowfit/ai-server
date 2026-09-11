# app/proto — 이제 비어 있다 (계약 원본은 루트 `proto/`)

2026-09-11 부터 gRPC 계약은 저장소 루트 **`proto/exercise.proto` 한 벌**이다. backend 의 Gradle 과
ai-server 의 이미지 빌드가 같은 파일에서 생성한다. 이 디렉터리엔 이 README 만 남아 있고, 옛 사본이
다시 생기면 **그것이 곧 드리프트다** — 여기 `.proto` 를 두지 말 것.

## 생성 산출물은 여전히 `ai-server/` 루트

`exercise_pb2.py`·`exercise_pb2_grpc.py` 는 `ai-server/` 루트에 커밋돼 있다. 생성기가 `-I ../proto` 로
불려서 생성물 내부 import 가 **bare**(`import exercise_pb2 as exercise__pb2`)이고, 이 이름은 `sys.path`
루트에서만 해석되기 때문이다 — 컨테이너의 `WORKDIR=/app` 이 곧 그 루트다. **위치는 취향이 아니라
import 규약의 결과**다(#132 에서 겪음).

- 이미지: Dockerfile 이 `/proto` 에서 **다시 생성**한다 — 커밋본을 쓰지 않는다
- 로컬 실행·pytest: 커밋본을 import 한다 → proto 를 고쳤으면 `./scripts/gen_proto.sh` 로 재생성해서 같이 커밋
- CI(`ai-server-test.yml`): 원본에서 재생성해 커밋본과 `git diff` — 다르면 실패

생성기 버전은 `requirements.txt` 의 `grpcio-tools==` 로 고정돼 있다(산출물이 버전에 묶이므로).
